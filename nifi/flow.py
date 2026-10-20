#!/usr/bin/env python3
"""Build the NiFi flows that carry wheels across a one-way link, and start them. Stdlib only.

usage: flow.py --side low|high|request-low|request-high [--url https://nifi:8443] [--user admin]
               [--password-stdin | --client-cert PEM --client-key PEM] [--ca-file PEM]
               [--param name=value] [--connect-diode GROUP] [--insecure] [--export FILE]

Bundles (mirror.py publish -t / export POST one tar):
low:  ListenHTTP :#{pypi.port}/contentListener -> PutFile #{pypi.dest}   (the diode's ingress directory)
high: ListFile #{pypi.source} -> FetchFile -> PutFile #{pypi.dest}       (the inbox `mirror.py import DIR` reads)

Requests (mirror.py request POSTs a small JSON list; nothing large is ever POSTed):
request-low:  ListenHTTP :#{request.port}/#{request.path} -> check X-Sha256 -> SplitJson, one FlowFile per
              wheel -> refuse any url outside #{gitlab.low.pypi.url} -> InvokeHTTP GET the wheel ->
              check its sha256 -> PackageFlowFile (every attribute rides inside the content) -> port "to diode"
request-high: port "from diode" -> UnpackContent flowfile-stream-v3 -> check the wheel's sha256 ->
              InvokeHTTP POST to #{gitlab.high.pypi.url} -> Uploaded, or Already in GitLab
--connect-diode GROUP wires "to diode" into GROUP's input port "pypi" (low), or GROUP's output port
"pypi" into "from diode" (high), and starts that port.

The NiFi password comes from the first line of stdin with --password-stdin, else from NIFI_PASSWORD;
it is never an argument, so it stays out of the process list and the shell history. GitLab tokens come
from GITLAB_LOW_TOKEN and GITLAB_HIGH_TOKEN and become sensitive parameters. With --client-cert and
--client-key, NiFi is reached with that certificate instead (a NiFiKop cluster's controller identity).

Failures loop back with NiFi's penalty, or wait in a stopped LogMessage "Rejected (inspect queue)", so a
stuck FlowFile stays visible. Started, it logs each one as it lets it go. Directories, ports and URLs live
in a parameter context.
"""
import argparse
import json
import os
import ssl
import sys
import time
import urllib.parse
import urllib.request

DEFAULTS = {"low": {"pypi.port": "9099", "pypi.dest": "/diode/pypi"},
            "high": {"pypi.source": "/diode/pypi", "pypi.dest": "/inbox/pypi"},
            "request-low": {"request.port": "9310", "request.path": "pypi-request", "gitlab.username": "__token__",
                            "gitlab.low.pypi.url": "https://gitlab.example.com/api/v4/projects/1/packages/pypi"},
            "request-high": {"gitlab.username": "__token__",
                             "gitlab.high.pypi.url": "https://gitlab.example.com/api/v4/projects/2/packages/pypi"}}
SECRETS = {"request-low": {"gitlab.low.token": "GITLAB_LOW_TOKEN"},
           "request-high": {"gitlab.high.token": "GITLAB_HIGH_TOKEN"}}


class Nifi:
    def __init__(self, url, user, password, verify_tls, ca_file=None, client_cert=None, client_key=None):
        self.api = url.rstrip("/") + "/nifi-api"
        self.user, self.password, self.token, self.types = user, password, None, {}
        self.ctx = ssl.create_default_context(cafile=ca_file)
        if client_cert:
            self.ctx.load_cert_chain(client_cert, client_key)
        if not verify_tls:
            self.ctx.check_hostname, self.ctx.verify_mode = False, ssl.CERT_NONE

    def login(self):
        if not self.password:
            return  # a client certificate identifies us; there is no token to fetch
        data = urllib.parse.urlencode({"username": self.user, "password": self.password}).encode()
        req = urllib.request.Request(self.api + "/access/token", data=data, method="POST",
                                     headers={"Content-Type": "application/x-www-form-urlencoded"})
        with urllib.request.urlopen(req, timeout=60, context=self.ctx) as r:
            self.token = r.read().decode()

    def call(self, method, path, body=None):
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        data = None
        if body is not None:
            data, headers["Content-Type"] = json.dumps(body).encode(), "application/json"
        req = urllib.request.Request(self.api + path, data=data, method=method, headers=headers)
        with urllib.request.urlopen(req, timeout=60, context=self.ctx) as r:
            raw = r.read()
            return json.loads(raw) if raw else None

    # Layout: a processor is 352 x 128 and a port 240 x 48. Rows leave room for the queue box
    # between them; a side column (Rejected) is reached through bends, never across a box.
    X_STEP, Y_STEP, BOX_W, BOX_H = 560, 240, 352, 128

    def place(self, component, x, y, w, h):
        if not hasattr(self, "positions"):
            self.positions = {}
        self.positions[component] = (x, y, w, h)

    def processor(self, pg, type_name, name, y, props, terminate=(), schedule=None, x=0, tasks=1):
        if not self.types:
            self.types = {t["type"]: t["bundle"] for t in self.call("GET", "/flow/processor-types")["processorTypes"]}
        full = next(t for t in self.types if t.endswith("." + type_name))
        cfg = {"properties": props, "autoTerminatedRelationships": list(terminate), "concurrentlySchedulableTaskCount": tasks}
        if schedule:
            cfg["schedulingPeriod"] = schedule
        position = {"x": x * self.X_STEP, "y": y * self.Y_STEP}
        created = self.call("POST", f"/process-groups/{pg}/processors",
                            {"revision": {"version": 0}, "component": {"type": full, "bundle": self.types[full], "name": name,
                                                                       "position": position, "config": cfg}})["id"]
        self.place(created, position["x"], position["y"], self.BOX_W, self.BOX_H)
        return created

    def port(self, pg, kind, name, y, x=0):
        """kind: input or output. Centred over the processor column."""
        position = {"x": x * self.X_STEP + (self.BOX_W - 240) // 2, "y": y * self.Y_STEP + 40}
        created = self.call("POST", f"/process-groups/{pg}/{kind}-ports",
                            {"revision": {"version": 0}, "component": {"name": name, "position": position}})["id"]
        self.place(created, position["x"], position["y"], 240, 48)
        return created

    def bends(self, src, dst):
        """None for a straight line down a column. A loop beside the box for a self connection. Else one
        bend: across at the source's height to the destination's centre line, then down into it."""
        positions = getattr(self, "positions", {})
        if src not in positions or dst not in positions:
            return []
        sx, sy, sw, sh = positions[src]
        dx, dy, dw, dh = positions[dst]
        if src == dst:
            return [{"x": sx + sw + 70, "y": sy + sh // 2 - 40}, {"x": sx + sw + 70, "y": sy + sh // 2 + 40}]
        if abs((sx + sw // 2) - (dx + dw // 2)) < 10:
            return []
        return [{"x": dx + dw // 2, "y": sy + sh // 2}]

    def connect(self, pg, src, dst, rels, src_type="PROCESSOR", dst_type="PROCESSOR", src_group=None, dst_group=None):
        component = {"source": {"id": src, "groupId": src_group or pg, "type": src_type},
                     "destination": {"id": dst, "groupId": dst_group or pg, "type": dst_type},
                     "selectedRelationships": list(rels)}
        if pg == (src_group or pg) == (dst_group or pg):
            bends = self.bends(src, dst)
            if bends:
                component["bends"], component["labelIndex"] = bends, 0
        self.call("POST", f"/process-groups/{pg}/connections", {"revision": {"version": 0}, "component": component})

    def set_state(self, kind, component, state):
        """Run or stop one processor or port (kind: processors, input-ports, output-ports)."""
        revision = self.call("GET", f"/{kind}/{component}")["revision"]
        self.call("PUT", f"/{kind}/{component}/run-status", {"revision": revision, "state": state})


def replace_group(n, root, name, params, secrets=None):
    """Remove a group of this name and its parameter context, then create both afresh."""
    for g in n.call("GET", f"/process-groups/{root}/process-groups")["processGroups"]:
        if g["component"]["name"] == name:
            n.call("PUT", f"/flow/process-groups/{g['id']}", {"id": g["id"], "state": "STOPPED"})
            for c in n.call("GET", f"/process-groups/{root}/connections")["connections"]:
                ends = (c["component"]["source"]["groupId"], c["component"]["destination"]["groupId"])
                if g["id"] in ends:  # a link to the diode: stop the far port, empty, then remove it
                    other = c["component"]["destination"] if ends[0] == g["id"] else c["component"]["source"]
                    kind = "input-ports" if other["type"] == "INPUT_PORT" else "output-ports"
                    n.set_state(kind, other["id"], "STOPPED")
                    n.call("POST", f"/flowfile-queues/{c['id']}/drop-requests")
                    time.sleep(1)
                    version = n.call("GET", f"/connections/{c['id']}")["revision"]["version"]
                    n.call("DELETE", f"/connections/{c['id']}?version={version}&clientId=pypi")
            time.sleep(2)
            n.call("POST", f"/process-groups/{g['id']}/empty-all-connections-requests")
            time.sleep(1)
            version = n.call("GET", f"/process-groups/{g['id']}")["revision"]["version"]
            n.call("DELETE", f"/process-groups/{g['id']}?version={version}&clientId=pypi")
    for c in n.call("GET", "/flow/parameter-contexts")["parameterContexts"]:
        if c["component"]["name"] == name:
            n.call("DELETE", f"/parameter-contexts/{c['id']}?version={c['revision']['version']}&clientId=pypi")
    ctx = n.call("POST", "/parameter-contexts", {"revision": {"version": 0}, "component": {
        "name": name, "parameters": [{"parameter": {"name": k, "value": v, "sensitive": False}} for k, v in params.items()]
        + [{"parameter": {"name": k, "value": v, "sensitive": True}} for k, v in (secrets or {}).items()]}})
    return n.call("POST", f"/process-groups/{root}/process-groups",
                  {"revision": {"version": 0},
                   "component": {"name": name, "position": {"x": 0, "y": 0}, "parameterContext": {"id": ctx["id"]}}})["id"]


# NiFi's expression language reads { and a backslash itself: no {n} quantifiers, no escapes.
WHEEL_NAME = "[A-Za-z0-9][A-Za-z0-9._+!-]*[.]whl"


def build_request_low(n, pg):
    """JSON request in, one packaged wheel per entry out of port "to diode"."""
    rejected = n.processor(pg, "LogMessage", "Rejected (inspect queue)", 13,
                           {"Log Level": "warn", "Log Message": "pypi request-low rejected ${filename} ${pypi.filename}"
                                                                " (HTTP ${invokehttp.status.code})"}, x=2, terminate=("success",))
    listen = n.processor(pg, "ListenHTTP", "Receive request", 0,
                         {"Listening Port": "#{request.port}", "Base Path": "#{request.path}",
                          "HTTP Headers for Attributes": "(?i)x-.*"})
    only = n.processor(pg, "RouteOnAttribute", "Python package requests only", 1,
                       {"Routing Strategy": "Route to Property name",
                        "request": "${X-Artifact-Type:equals('python-packages-request'):and(${X-Artifact-Format:equals('json')})}"})
    hash_request = n.processor(pg, "CryptographicHashContent", "Hash request", 2, {"Hash Algorithm": "SHA-256"})
    request_ok = n.processor(pg, "RouteOnAttribute", "Request matches X-Sha256", 3,
                             {"Routing Strategy": "Route to Property name",
                              "verified": "${content_SHA-256:equals(${X-Sha256})}"})
    split = n.processor(pg, "SplitJson", "One FlowFile per wheel", 4, {"JsonPath Expression": "$.files.*"},
                        terminate=("original",))
    fields = n.processor(pg, "EvaluateJsonPath", "Wheel fields", 5,
                         {"Destination": "flowfile-attribute", "Return Type": "scalar",
                          "pypi.filename": "$.filename", "pypi.name": "$.name", "pypi.version": "$.version",
                          "pypi.sha256": "$.sha256", "pypi.size": "$.size", "pypi.url": "$.url"})
    # NiFi does not put a parameter into a quoted literal of an expression, so the base comes in as an
    # attribute, set here after the request's own fields so a request cannot choose it.
    base = n.processor(pg, "UpdateAttribute", "Low registry base", 6, {"pypi.allowed.base": "#{gitlab.low.pypi.url}/files/"})
    # The url comes from the request: fetch only from the low registry, and only a wheel by a plain name.
    allowed = n.processor(pg, "RouteOnAttribute", "From the low registry only", 7,
                          {"Routing Strategy": "Route to Property name",
                           "allowed": "${pypi.url:startsWith(${pypi.allowed.base})"
                                      f":and(${{pypi.filename:matches('{WHEEL_NAME}')}})"
                                      ":and(${pypi.sha256:length():equals(64)})"
                                      ":and(${pypi.sha256:matches('[0-9a-f]+')})}"})
    fetch = n.processor(pg, "InvokeHTTP", "Fetch wheel", 8,
                        {"HTTP Method": "GET", "HTTP URL": "${pypi.url}", "Request Username": "#{gitlab.username}",
                         "Request Password": "#{gitlab.low.token}", "Connection Timeout": "30 secs",
                         "Socket Read Timeout": "5 mins", "Response Generation Required": "false"},
                        terminate=("Original",), tasks=2)
    hash_wheel = n.processor(pg, "CryptographicHashContent", "Hash wheel", 9, {"Hash Algorithm": "SHA-256"})
    wheel_ok = n.processor(pg, "RouteOnAttribute", "Wheel matches its sha256", 10,
                           {"Routing Strategy": "Route to Property name",
                            "verified": "${content_SHA-256:equals(${pypi.sha256})}"})
    label = n.processor(pg, "UpdateAttribute", "Label wheel", 11,
                        {"filename": "${pypi.filename}", "X-Sha256": "${pypi.sha256}",
                         "X-Artifact-Type": "python-packages", "X-Artifact-Format": "whl", "X-Artifact-Action": "mirror",
                         "mime.type": "application/octet-stream",
                         "Delete Attributes Expression": r"invokehttp\..*|content_SHA-256|X-File-Count|restlistener\..*|pypi\.allowed\.base"})
    # A link may keep only the content: every attribute rides inside it, and the high side unpacks it.
    package = n.processor(pg, "PackageFlowFile", "Keep attributes across the link", 12, {}, terminate=("original",))
    out = n.port(pg, "output", "to diode", 13)
    n.connect(pg, listen, only, ["success"])
    n.connect(pg, only, hash_request, ["request"])
    n.connect(pg, only, rejected, ["unmatched"])
    n.connect(pg, hash_request, request_ok, ["success"])
    n.connect(pg, request_ok, split, ["verified"])
    n.connect(pg, split, fields, ["split"])
    n.connect(pg, fields, base, ["matched"])
    n.connect(pg, base, allowed, ["success"])
    n.connect(pg, allowed, fetch, ["allowed"])
    n.connect(pg, fetch, hash_wheel, ["Response"])
    n.connect(pg, fetch, fetch, ["Retry"])
    n.connect(pg, hash_wheel, wheel_ok, ["success"])
    n.connect(pg, wheel_ok, label, ["verified"])
    n.connect(pg, label, package, ["success"])
    n.connect(pg, package, out, ["success"], dst_type="OUTPUT_PORT")
    for source, rels in ((hash_request, ["failure"]), (request_ok, ["unmatched"]), (split, ["failure"]),
                         (fields, ["unmatched", "failure"]), (allowed, ["unmatched"]), (fetch, ["No Retry", "Failure"]),
                         (hash_wheel, ["failure"]), (wheel_ok, ["unmatched"])):
        n.connect(pg, source, rejected, rels)
    return rejected, out


def build_request_high(n, pg):
    """Packaged wheels in from port "from diode", each uploaded to the high registry."""
    rejected = n.processor(pg, "LogMessage", "Rejected (inspect queue)", 8,
                           {"Log Level": "warn", "Log Message": "pypi request-high rejected ${filename}"
                                                                " (HTTP ${invokehttp.status.code}) ${gitlab.response}"}, x=2, terminate=("success",))
    done = n.processor(pg, "UpdateAttribute", "Uploaded", 7, {}, terminate=("success",))
    inp = n.port(pg, "input", "from diode", 0)
    unpack = n.processor(pg, "UnpackContent", "Restore attributes", 1, {"Packaging Format": "flowfile-stream-v3"},
                         terminate=("original",))
    only = n.processor(pg, "RouteOnAttribute", "Wheels only", 2,
                       {"Routing Strategy": "Route to Property name",
                        "wheel": "${X-Artifact-Type:equals('python-packages'):and(${X-Artifact-Format:equals('whl')})"
                                 f":and(${{filename:matches('{WHEEL_NAME}')}})}}"})
    hash_wheel = n.processor(pg, "CryptographicHashContent", "Hash wheel", 3, {"Hash Algorithm": "SHA-256"})
    wheel_ok = n.processor(pg, "RouteOnAttribute", "Wheel matches X-Sha256", 4,
                           {"Routing Strategy": "Route to Property name",
                            "verified": "${content_SHA-256:equals(${X-Sha256})}"})
    upload = n.processor(pg, "InvokeHTTP", "Upload to GitLab", 5,
                         {"HTTP Method": "POST", "HTTP URL": "#{gitlab.high.pypi.url}", "Request Username": "#{gitlab.username}",
                          "Request Password": "#{gitlab.high.token}", "Request Multipart Form-Data Name": "content",
                          "Request Multipart Form-Data Filename Enabled": "true", "Request Content-Type": "${mime.type}",
                          "post:form:name": "${pypi.name}", "post:form:version": "${pypi.version}",
                          "post:form:sha256_digest": "${content_SHA-256}", "Response Body Attribute Name": "gitlab.response",
                          "Connection Timeout": "30 secs", "Socket Read Timeout": "5 mins", "Socket Write Timeout": "5 mins"},
                         terminate=("Response",))
    duplicate = n.processor(pg, "RouteOnAttribute", "Already in GitLab", 6,
                            {"Routing Strategy": "Route to Property name",
                             "duplicate": "${invokehttp.status.code:equals('400'):and(${gitlab.response:contains('taken')})}"}, x=1)
    n.connect(pg, inp, unpack, [], src_type="INPUT_PORT")
    n.connect(pg, unpack, only, ["success"])
    n.connect(pg, only, hash_wheel, ["wheel"])
    n.connect(pg, hash_wheel, wheel_ok, ["success"])
    n.connect(pg, wheel_ok, upload, ["verified"])
    n.connect(pg, upload, upload, ["Retry"])
    n.connect(pg, upload, done, ["Original"])
    n.connect(pg, upload, duplicate, ["No Retry"])
    n.connect(pg, duplicate, done, ["duplicate"])
    for source, rels in ((unpack, ["failure"]), (only, ["unmatched"]), (hash_wheel, ["failure"]),
                         (wheel_ok, ["unmatched"]), (upload, ["Failure"]), (duplicate, ["unmatched"])):
        n.connect(pg, source, rejected, rels)
    return rejected, inp


def connect_diode(n, root, pg, side, port, diode_name):
    """Wire our port to the diode group's port named pypi, at the root, and start the diode's port."""
    group = next(g for g in n.call("GET", f"/process-groups/{root}/process-groups")["processGroups"]
                 if g["component"]["name"] == diode_name)
    # Beside the diode group on the root canvas: low to its left, high to its right.
    at = group["component"]["position"]
    revision = n.call("GET", f"/process-groups/{pg}")["revision"]
    n.call("PUT", f"/process-groups/{pg}", {"revision": revision, "component": {
        "id": pg, "position": {"x": at["x"] + (-n.X_STEP if side == "request-low" else n.X_STEP), "y": at["y"]}}})
    kind = "input-ports" if side == "request-low" else "output-ports"
    far = next(p for p in n.call("GET", f"/process-groups/{group['id']}/{kind}")[kind.replace("-p", "P")]
               if p["component"]["name"] == "pypi")
    if side == "request-low":
        n.connect(root, port, far["id"], [], src_type="OUTPUT_PORT", dst_type="INPUT_PORT",
                  src_group=pg, dst_group=group["id"])
    else:
        n.connect(root, far["id"], port, [], src_type="OUTPUT_PORT", dst_type="INPUT_PORT",
                  src_group=group["id"], dst_group=pg)
    n.set_state(kind, far["id"], "RUNNING")


def build(n, side, params, secrets=None, diode=None):
    root = n.call("GET", "/flow/process-groups/root")["processGroupFlow"]["id"]
    if side.startswith("request-"):
        pg = replace_group(n, root, f"pypi {side}", params, secrets)
        rejected, port = (build_request_low if side == "request-low" else build_request_high)(n, pg)
        if diode:
            connect_diode(n, root, pg, side, port, diode)
        n.call("PUT", f"/flow/process-groups/{pg}", {"id": pg, "state": "RUNNING"})
        n.set_state("processors", rejected, "STOPPED")  # its queue is the record of what failed
        return pg
    pg = replace_group(n, root, f"pypi {side} side", params)
    put = n.processor(pg, "PutFile", "hand over", 2,
                      {"Directory": "#{pypi.dest}", "Conflict Resolution Strategy": "fail",
                       "Create Missing Directories": "true"}, terminate=("success",))
    n.connect(pg, put, put, ["failure"])
    if side == "low":
        listen = n.processor(pg, "ListenHTTP", "receive bundle", 0,
                             {"Listening Port": "#{pypi.port}", "Base Path": "contentListener",
                              # ListenHTTP takes the Filename header as the filename on its own;
                              # X-Sha256, X-Artifact-* and X-Bundle-Kind become attributes
                              "HTTP Headers for Attributes": "(?i)x-.*"})
        n.connect(pg, listen, put, ["success"])
    else:
        ls = n.processor(pg, "ListFile", "list bundles", 0,
                         {"Input Directory": "#{pypi.source}", "File Filter": r"pypi-.*\.tar",
                          "Recurse Subdirectories": "false", "Minimum File Age": "5 sec",
                          "Ignore Hidden Files": "true"}, schedule="10 sec")
        fetch = n.processor(pg, "FetchFile", "take bundle", 1,
                            {"Completion Strategy": "Move File", "Move Destination Directory": "#{pypi.source}/sent",
                             "Move Conflict Strategy": "Replace File"}, terminate=("not.found",))
        n.connect(pg, ls, fetch, ["success"])
        n.connect(pg, fetch, put, ["success"])
        n.connect(pg, fetch, fetch, ["failure", "permission.denied"])
    n.call("PUT", f"/flow/process-groups/{pg}", {"id": pg, "state": "RUNNING"})
    return pg


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0], allow_abbrev=False)
    ap.add_argument("--side", choices=tuple(DEFAULTS), required=True)
    ap.add_argument("--url", default=os.environ.get("NIFI_API_URL", "https://localhost:8443"))
    ap.add_argument("--user", default=os.environ.get("NIFI_USER", "admin"))
    ap.add_argument("--password-stdin", action="store_true", help="read the password from the first line of stdin (else NIFI_PASSWORD)")
    ap.add_argument("--param", action="append", default=[], help="pypi.port=, pypi.source= or pypi.dest=")
    ap.add_argument("--insecure", action="store_true", help="skip TLS verification (self-signed NiFi)")
    ap.add_argument("--ca-file", help="CA bundle that signed NiFi's certificate")
    ap.add_argument("--client-cert", help="log in with this client certificate (PEM) instead of a password")
    ap.add_argument("--client-key", help="the client certificate's key (PEM)")
    ap.add_argument("--connect-diode", metavar="GROUP",
                    help="request sides: wire to the port named pypi on this root process group and start it")
    ap.add_argument("--export", help="also write the group's flow definition to this file")
    args = ap.parse_args(argv)
    password = sys.stdin.readline().rstrip("\r\n") if args.password_stdin else os.environ.get("NIFI_PASSWORD", "")
    if not password and not args.client_cert:
        ap.error("the password is empty: pipe it to --password-stdin, set NIFI_PASSWORD, or use --client-cert")
    params = dict(DEFAULTS[args.side], **dict(p.split("=", 1) for p in args.param))
    secrets = {name: os.environ.get(variable, "") for name, variable in SECRETS.get(args.side, {}).items()}
    empty = [SECRETS[args.side][name] for name, value in secrets.items() if not value]
    if empty:
        ap.error(f"set {', '.join(empty)}: the GitLab token becomes a sensitive parameter")
    n = Nifi(args.url, args.user, "" if args.client_cert else password, not args.insecure,
             args.ca_file, args.client_cert, args.client_key)
    n.login()
    pg = build(n, args.side, params, secrets, args.connect_diode)
    print(f"pypi {args.side} running in process group {pg}: {params}")
    if args.export:
        req = urllib.request.Request(f"{n.api}/process-groups/{pg}/download", headers={"Authorization": f"Bearer {n.token}"})
        with urllib.request.urlopen(req, timeout=60, context=n.ctx) as r:
            open(args.export, "wb").write(r.read())
        print(f"flow definition written to {args.export}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
