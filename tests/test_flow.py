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
            def __init__(self, url, user, password, verify):
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
