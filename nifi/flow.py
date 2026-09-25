#!/usr/bin/env python3
"""Build the NiFi flow that carries pypi-*.tar bundles across a one-way link, and start it. Stdlib only.

usage: flow.py --side low|high [--url https://nifi:8443] [--user admin] [--password ...]
               [--param pypi.port=9099] [--param pypi.dest=/dir] [--insecure] [--export FILE]

low:  ListenHTTP :#{pypi.port}/contentListener  (the sync job POSTs each bundle here, NIFI_URL)
        -> PutFile #{pypi.dest}                  (the diode's ingress directory)
high: ListFile #{pypi.source}                    (the diode's egress directory)
        -> FetchFile                             (moves the original to #{pypi.source}/sent)
        -> PutFile #{pypi.dest}                  (the inbox `mirror.py import --inbox` reads)

The bundle crosses byte for byte; `mirror.py import` verifies it. PutFile fails on a name
conflict, so a re-sent bundle never overwrites silently. Failures loop back with NiFi's penalty,
so a stuck file stays visible in the queue. Directories and the port live in a parameter context.
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
            "high": {"pypi.source": "/diode/pypi", "pypi.dest": "/inbox/pypi"}}


class Nifi:
    def __init__(self, url, user, password, verify_tls):
        self.api = url.rstrip("/") + "/nifi-api"
        self.user, self.password, self.token, self.types = user, password, None, {}
        self.ctx = ssl.create_default_context()
        if not verify_tls:
            self.ctx.check_hostname, self.ctx.verify_mode = False, ssl.CERT_NONE

    def login(self):
        data = urllib.parse.urlencode({"username": self.user, "password": self.password}).encode()
        req = urllib.request.Request(self.api + "/access/token", data=data, method="POST",
                                     headers={"Content-Type": "application/x-www-form-urlencoded"})
        with urllib.request.urlopen(req, timeout=60, context=self.ctx) as r:
            self.token = r.read().decode()

    def call(self, method, path, body=None):
        headers = {"Authorization": f"Bearer {self.token}"}
        data = None
        if body is not None:
            data, headers["Content-Type"] = json.dumps(body).encode(), "application/json"
        req = urllib.request.Request(self.api + path, data=data, method=method, headers=headers)
        with urllib.request.urlopen(req, timeout=60, context=self.ctx) as r:
            raw = r.read()
            return json.loads(raw) if raw else None

    def processor(self, pg, type_name, name, y, props, terminate=(), schedule=None):
        if not self.types:
            self.types = {t["type"]: t["bundle"] for t in self.call("GET", "/flow/processor-types")["processorTypes"]}
        full = next(t for t in self.types if t.endswith("." + type_name))
        cfg = {"properties": props, "autoTerminatedRelationships": list(terminate)}
        if schedule:
            cfg["schedulingPeriod"] = schedule
        return self.call("POST", f"/process-groups/{pg}/processors",
                         {"revision": {"version": 0}, "component": {"type": full, "bundle": self.types[full], "name": name,
                                                                    "position": {"x": 0, "y": y * 180}, "config": cfg}})["id"]

    def connect(self, pg, src, dst, rels):
        self.call("POST", f"/process-groups/{pg}/connections",
                  {"revision": {"version": 0},
                   "component": {"source": {"id": src, "groupId": pg, "type": "PROCESSOR"},
                                 "destination": {"id": dst, "groupId": pg, "type": "PROCESSOR"},
                                 "selectedRelationships": list(rels)}})


def replace_group(n, root, name, params):
    """Remove a group of this name and its parameter context, then create both afresh."""
    for g in n.call("GET", f"/process-groups/{root}/process-groups")["processGroups"]:
        if g["component"]["name"] == name:
            n.call("PUT", f"/flow/process-groups/{g['id']}", {"id": g["id"], "state": "STOPPED"})
            time.sleep(2)
            n.call("POST", f"/process-groups/{g['id']}/empty-all-connections-requests")
            time.sleep(1)
            version = n.call("GET", f"/process-groups/{g['id']}")["revision"]["version"]
            n.call("DELETE", f"/process-groups/{g['id']}?version={version}&clientId=pypi")
    for c in n.call("GET", "/flow/parameter-contexts")["parameterContexts"]:
        if c["component"]["name"] == name:
            n.call("DELETE", f"/parameter-contexts/{c['id']}?version={c['revision']['version']}&clientId=pypi")
    ctx = n.call("POST", "/parameter-contexts", {"revision": {"version": 0}, "component": {
        "name": name, "parameters": [{"parameter": {"name": k, "value": v, "sensitive": False}} for k, v in params.items()]}})
    return n.call("POST", f"/process-groups/{root}/process-groups",
                  {"revision": {"version": 0},
                   "component": {"name": name, "position": {"x": 0, "y": 0}, "parameterContext": {"id": ctx["id"]}}})["id"]


def build(n, side, params):
    root = n.call("GET", "/flow/process-groups/root")["processGroupFlow"]["id"]
    pg = replace_group(n, root, f"pypi {side} side", params)
    put = n.processor(pg, "PutFile", "hand over", 2,
                      {"Directory": "#{pypi.dest}", "Conflict Resolution Strategy": "fail",
                       "Create Missing Directories": "true"}, terminate=("success",))
    n.connect(pg, put, put, ["failure"])
    if side == "low":
        listen = n.processor(pg, "ListenHTTP", "receive bundle", 0,
                             {"Listening Port": "#{pypi.port}", "Base Path": "contentListener",
                              # the sender's filename header replaces NiFi's generated one
                              "HTTP Headers to receive as Attributes (Regex)": "filename|x-sha256"})
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


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--side", choices=("low", "high"), required=True)
    ap.add_argument("--url", default=os.environ.get("NIFI_API_URL", "https://localhost:8443"))
    ap.add_argument("--user", default=os.environ.get("NIFI_USER", "admin"))
    ap.add_argument("--password", default=os.environ.get("NIFI_PASSWORD"))
    ap.add_argument("--param", action="append", default=[], help="pypi.port=, pypi.source= or pypi.dest=")
    ap.add_argument("--insecure", action="store_true", help="skip TLS verification (self-signed NiFi)")
    ap.add_argument("--export", help="also write the group's flow definition to this file")
    args = ap.parse_args()
    if not args.password:
        raise SystemExit("set --password or NIFI_PASSWORD")
    params = dict(DEFAULTS[args.side], **dict(p.split("=", 1) for p in args.param))
    n = Nifi(args.url, args.user, args.password, not args.insecure)
    n.login()
    pg = build(n, args.side, params)
    print(f"pypi {args.side} side running in process group {pg}: {params}")
    if args.export:
        req = urllib.request.Request(f"{n.api}/process-groups/{pg}/download", headers={"Authorization": f"Bearer {n.token}"})
        with urllib.request.urlopen(req, timeout=60, context=n.ctx) as r:
            open(args.export, "wb").write(r.read())
        print(f"flow definition written to {args.export}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
