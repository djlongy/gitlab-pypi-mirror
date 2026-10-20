"""nifi/flow.py takes the NiFi password from stdin or the environment, never from argv."""

import importlib.util
import io
import os
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

spec = importlib.util.spec_from_file_location("flow", Path(__file__).resolve().parents[1] / "nifi" / "flow.py")
flow = importlib.util.module_from_spec(spec)
spec.loader.exec_module(flow)


class PasswordTests(unittest.TestCase):
    def run_main(self, *argv, stdin="", env=None):
        """flow.main with a fake NiFi: returns (exit code, stderr, the credentials the client was built with)."""
        built = []

        class FakeNifi:
            def __init__(self, url, user, password, verify, *certificate):
                built.append((url, user, password, verify))

            def login(self):
                pass

        err = io.StringIO()
        with mock.patch.dict(os.environ, env or {}, clear=True), mock.patch.object(sys, "stdin", io.StringIO(stdin)), \
                mock.patch.object(flow, "Nifi", FakeNifi), mock.patch.object(flow, "build", return_value="pg-1"), \
                redirect_stderr(err), redirect_stdout(io.StringIO()):
            try:
                code = flow.main(list(argv))
            except SystemExit as stop:
                code = stop.code if isinstance(stop.code, int) else 1
                if isinstance(stop.code, str):
                    err.write(stop.code)
        return code, err.getvalue(), built

    def test_password_on_the_command_line_is_refused(self):
        code, err, built = self.run_main("--side", "low", "--password", "s3cret", env={"NIFI_PASSWORD": "env"})
        self.assertEqual(code, 2)
        self.assertEqual(built, [])
        self.assertIn("unrecognized arguments: --password", err)

    def test_password_stdin_reads_one_line(self):
        code, err, built = self.run_main("--side", "low", "--password-stdin", stdin="from-stdin\nsecond\n")
        self.assertEqual(code, 0, err)
        self.assertEqual(built[0][2], "from-stdin")

    def test_the_environment_supplies_the_password(self):
        code, err, built = self.run_main("--side", "high", env={"NIFI_PASSWORD": "from-env"})
        self.assertEqual(code, 0, err)
        self.assertEqual(built[0][2], "from-env")

    def test_stdin_wins_over_the_environment(self):
        code, err, built = self.run_main("--side", "low", "--password-stdin", stdin="stdin-pw\n", env={"NIFI_PASSWORD": "env"})
        self.assertEqual(built[0][2], "stdin-pw")

    def test_no_password_is_a_usage_error_naming_both_sources(self):
        code, err, built = self.run_main("--side", "low")
        self.assertEqual(code, 2)
        self.assertIn("--password-stdin", err)
        self.assertIn("NIFI_PASSWORD", err)
        self.assertEqual(built, [])

    def test_an_empty_stdin_is_a_usage_error(self):
        code, err, built = self.run_main("--side", "low", "--password-stdin", stdin="")
        self.assertEqual(code, 2)
        self.assertEqual(built, [])


if __name__ == "__main__":
    unittest.main()


class RequestFlowTests(unittest.TestCase):
    """The request flows are built through the NiFi API; this records what would be created."""

    class Recorder:
        def __init__(self):
            self.processors, self.connections = {}, []

        def processor(self, pg, type_name, name, y, props, terminate=(), schedule=None, x=0, tasks=1):
            self.processors[name] = (type_name, props, set(terminate))
            return name

        def port(self, pg, kind, name, y):
            self.processors[name] = (f"{kind} port", {}, set())
            return name

        def connect(self, pg, src, dst, rels, **kinds):
            self.connections.append((src, dst, tuple(rels)))

    def built(self, side):
        n = self.Recorder()
        (flow.build_request_low if side == "low" else flow.build_request_high)(n, "pg")
        return n

    def test_every_expression_is_balanced_and_no_parameter_sits_in_a_quoted_literal(self):
        for side in ("low", "high"):
            for name, (_, props, _) in self.built(side).processors.items():
                for key, value in props.items():
                    if "${" not in value:
                        continue
                    with self.subTest(side=side, processor=name, property=key):
                        self.assertEqual(value.count("{"), value.count("}"), value)
                        self.assertEqual(value.count("("), value.count(")"), value)
                        # NiFi leaves #{...} untouched inside '...' of an expression
                        self.assertNotRegex(value, r"'[^']*#\{[^']*'")

    def test_every_relationship_ends_somewhere(self):
        relationships = {"ListenHTTP": {"success"}, "RouteOnAttribute": {"unmatched"}, "CryptographicHashContent": {"success", "failure"},
                         "SplitJson": {"split", "original", "failure"}, "EvaluateJsonPath": {"matched", "unmatched", "failure"},
                         "InvokeHTTP": {"Response", "Original", "Retry", "No Retry", "Failure"}, "UpdateAttribute": {"success"},
                         "LogMessage": {"success"}, "PackageFlowFile": {"success", "original"}, "UnpackContent": {"success", "original", "failure"}}
        for side in ("low", "high"):
            n = self.built(side)
            for name, (type_name, props, terminated) in n.processors.items():
                if type_name.endswith("port"):
                    continue
                wanted = relationships[type_name] | ({k for k in props if k in ("request", "verified", "allowed", "wheel", "duplicate")}
                                                     if type_name == "RouteOnAttribute" else set())
                used = {r for src, _, rels in n.connections if src == name for r in rels} | terminated
                with self.subTest(side=side, processor=name):
                    self.assertEqual(wanted - used, set())

    def test_the_low_side_fetches_only_from_its_own_registry(self):
        n = self.built("low")
        self.assertEqual(n.processors["Low registry base"][1], {"pypi.allowed.base": "#{gitlab.low.pypi.url}/files/"})
        self.assertIn("startsWith(${pypi.allowed.base})", n.processors["From the low registry only"][1]["allowed"])
        self.assertIn(("Wheel fields", "Low registry base", ("matched",)), n.connections)
        self.assertIn(("Low registry base", "From the low registry only", ("success",)), n.connections)

    def test_every_failure_waits_in_one_logmessage(self):
        for side in ("low", "high"):
            n = self.built(side)
            with self.subTest(side=side):
                self.assertEqual(n.processors["Rejected (inspect queue)"][0], "LogMessage")
                ends = {dst for _, dst, rels in n.connections if set(rels) & {"failure", "Failure", "unmatched"}}
                self.assertEqual(ends, {"Rejected (inspect queue)"})

    def test_a_request_side_needs_its_gitlab_token(self):
        code, err, _ = PasswordTests.run_main(PasswordTests(), "--side", "request-high", env={"NIFI_PASSWORD": "pw"})
        self.assertEqual(code, 2)
        self.assertIn("set GITLAB_HIGH_TOKEN", err)
        code, err, built = PasswordTests.run_main(PasswordTests(), "--side", "request-high", "--client-cert", "c.pem",
                                                   env={"GITLAB_HIGH_TOKEN": "t"})
        self.assertEqual(code, 0, err)
        self.assertEqual(built[0][2], "", "a client certificate needs no password")



class LayoutTests(unittest.TestCase):
    """The flows are laid out so no box overlaps another and no connection runs through a box."""

    class Fake(flow.Nifi):
        def __init__(self):
            super().__init__("https://nifi", "", "", True)
            self.types = {"org.apache.nifi.processors.standard.X": {}}
            self.made, self.lines, self.names = 0, [], {}

        def call(self, method, path, body=None):
            if path.endswith("/connections"):
                c = body["component"]
                self.lines.append((c["source"]["id"], c["destination"]["id"], c.get("bends", [])))
                return {}
            self.made += 1
            self.names[f"c{self.made}"] = body["component"]["name"]
            return {"id": f"c{self.made}"}

        def processor(self, pg, type_name, name, y, props, **kw):
            self.types = {f"org.apache.nifi.processors.standard.{type_name}": {}}
            return super().processor(pg, type_name, name, y, props, **kw)

    @staticmethod
    def crosses(a, b, box):
        """Whether segment a-b enters the rectangle box (x, y, w, h), sampled along its length."""
        x, y, w, h = box
        for i in range(1, 100):
            px, py = a[0] + (b[0] - a[0]) * i / 100, a[1] + (b[1] - a[1]) * i / 100
            if x + 2 < px < x + w - 2 and y + 2 < py < y + h - 2:
                return True
        return False

    def test_no_box_overlaps_and_no_line_crosses_a_box(self):
        for side in ("low", "high"):
            n = self.Fake()
            (flow.build_request_low if side == "low" else flow.build_request_high)(n, "pg")
            boxes = n.positions
            items = list(boxes.items())
            for i, (a, (ax, ay, aw, ah)) in enumerate(items):
                for b, (bx, by, bw, bh) in items[i + 1:]:
                    with self.subTest(side=side, overlap=(n.names[a], n.names[b])):
                        self.assertFalse(ax < bx + bw and bx < ax + aw and ay < by + bh and by < ay + ah)
            centre = lambda c: (boxes[c][0] + boxes[c][2] / 2, boxes[c][1] + boxes[c][3] / 2)
            for src, dst, bends in n.lines:
                if src == dst:
                    self.assertEqual(len(bends), 2, "a self loop needs two bends to be drawn")
                    continue
                points = [centre(src)] + [(p["x"], p["y"]) for p in bends] + [centre(dst)]
                for other, box in boxes.items():
                    if other in (src, dst):
                        continue
                    for a, b in zip(points, points[1:]):
                        with self.subTest(side=side, line=(n.names[src], n.names[dst]), box=n.names[other]):
                            self.assertFalse(self.crosses(a, b, box))
