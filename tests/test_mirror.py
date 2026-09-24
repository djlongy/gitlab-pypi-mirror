"""Tests for mirror.py. Standard library only: python3 -m unittest discover -s tests

The registry tests run against a small fake of GitLab's PyPI endpoints, so the
skip-existing logic is exercised end to end without a GitLab instance.
"""

import base64
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import zipfile
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import mirror  # noqa: E402

TOKEN = "t0ken"
# The test environment is cleared, so the shell tests get a PATH of their own.
SHELL_PATH = os.pathsep.join(os.path.dirname(shutil.which(t) or "/usr/bin") for t in ("curl", "unzip", "sha256sum", "sh"))
UPSTREAM_FILES = ["idna-3.20-py3-none-any.whl", "requests-2.32.5-py3-none-any.whl"]


class FakeGitLab(BaseHTTPRequestHandler):
    """GET .../packages/pypi/simple/<name> and POST .../packages/pypi."""

    files = {}  # normalized name -> {filename: form fields}

    def log_message(self, *args):
        pass

    def _authorized(self):
        expected = "Basic " + base64.b64encode(f"gitlab-ci-token:{TOKEN}".encode()).decode()
        if self.headers.get("Authorization") != expected:
            self.send_response(401)
            self.end_headers()
            return False
        return True

    def do_GET(self):
        if self.path.startswith("/upstream/"):
            links = "".join(f'<a href="x">{n}</a>' for n in UPSTREAM_FILES)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(links.encode())
            return
        if not self._authorized():
            return
        match = re.search(r"/packages/pypi/simple/([^/]+)$", self.path)
        entries = self.files.get(match[1]) if match else None
        if not entries and self.server.forward:
            # GitLab's default: a name the project does not hold is forwarded
            # to pypi.org, whose page lists every file ever released.
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{self.server.server_port}/upstream/{match[1]}")
            self.end_headers()
            return
        if not entries:
            self.send_response(404)
            self.end_headers()
            return
        links = "".join(f'<a href="files/x/{n}#sha256=0">{n}</a><br>' for n in entries)
        body = f"<html><body>{links}</body></html>".encode()
        self.send_response(200)
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if not self._authorized():
            return
        length = int(self.headers["Content-Length"])
        raw = self.rfile.read(length)
        boundary = self.headers["Content-Type"].split("boundary=")[1].encode()
        fields, filename = {}, None
        for part in raw.split(b"--" + boundary):
            if b"Content-Disposition" not in part:
                continue
            head, _, value = part.partition(b"\r\n\r\n")
            name = re.search(rb'name="([^"]+)"', head)[1].decode()
            fname = re.search(rb'filename="([^"]+)"', head)
            value = value[: -len(b"\r\n")] if value.endswith(b"\r\n") else value
            if fname:
                filename = fname[1].decode()
            else:
                fields[name] = value.decode()
        key = mirror.normalize(fields["name"])
        bucket = self.files.setdefault(key, {})
        if filename in bucket:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b'{"message":"Validation failed: File name has already been taken"}')
            return
        bucket[filename] = fields
        self.send_response(201)
        self.end_headers()


def make_wheel(directory: Path, filename: str, requires_python: str = "") -> Path:
    name, version = mirror.name_and_version(filename)
    path = directory / filename
    metadata = f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n"
    if requires_python:
        metadata += f"Requires-Python: {requires_python}\n"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(f"{name}-{version}.dist-info/METADATA", metadata)
    return path


class Workspace(unittest.TestCase):
    """A throwaway packages/ tree with mirror.ROOT pointed at it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        patcher = mock.patch.multiple(mirror, ROOT=root, PACKAGES=root / "packages")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)
        self.packages = root / "packages"
        self.packages.mkdir()

    def target(self, name: str) -> Path:
        path = self.packages / name / "wheelhouse"
        path.mkdir(parents=True)
        return path


class TargetTests(Workspace):
    def test_directory_names_are_read_as_os_python_and_arch(self):
        (self.packages / "linux-py3.12").mkdir()
        (self.packages / "windows-py3.9-arm64").mkdir()
        (self.packages / "linux-py3.14-aarch64").mkdir()
        got = {t.name: (t.os, t.python, t.arch, t.platforms[0]) for t in mirror.find_targets()}
        self.assertEqual(got["linux-py3.12"], ("linux", "3.12", "x86_64", "manylinux_2_28_x86_64"))
        self.assertEqual(got["windows-py3.9-arm64"], ("windows", "3.9", "arm64", "win_arm64"))
        self.assertEqual(got["linux-py3.14-aarch64"][3], "manylinux_2_28_aarch64")

    def test_linux_platforms_include_every_older_glibc(self):
        """pip does not expand manylinux_2_28 itself: without the older tags a
        manylinux2014-only wheel is invisible and the download fails."""
        tags = mirror.linux_platforms("2_28", "x86_64")
        self.assertEqual(tags[0], "manylinux_2_28_x86_64")
        self.assertIn("manylinux_2_17_x86_64", tags)
        self.assertIn("manylinux2014_x86_64", tags)
        self.assertNotIn("manylinux2010_aarch64", mirror.linux_platforms("2_28", "aarch64"))

    def test_a_platforms_file_overrides_the_defaults(self):
        target = self.packages / "linux-py3.12"
        target.mkdir()
        (target / "platforms.txt").write_text("# RHEL 7\nmanylinux2014_x86_64\n")
        self.assertEqual(mirror.find_targets()[0].platforms, ["manylinux2014_x86_64"])

    def test_a_misnamed_directory_is_refused(self):
        for bad in ("py3.12-linux", "linux-3.12", "macos-py3.12", "linux-py3.12-sparc"):
            with self.subTest(bad):
                path = self.packages / bad
                path.mkdir()
                with self.assertRaises(mirror.MirrorError):
                    mirror.find_targets()
                path.rmdir()


class FileNameTests(unittest.TestCase):
    def test_wheel_names(self):
        cases = {
            "PyYAML-6.0.2-cp39-cp39-manylinux_2_17_x86_64.manylinux2014_x86_64.whl": ("PyYAML", "6.0.2"),
            "pywin32-312-cp312-cp312-win_amd64.whl": ("pywin32", "312"),
            "foo-1.0-1-py3-none-any.whl": ("foo", "1.0"),
        }
        for filename, expected in cases.items():
            with self.subTest(filename):
                self.assertEqual(mirror.name_and_version(filename), expected)

    def test_a_source_distribution_is_not_a_package_name(self):
        for filename in ("python-dateutil-2.8.2.tar.gz", "pkg-0.1.zip", "pkg-0.1-py3.whl"):
            with self.subTest(filename):
                with self.assertRaises(mirror.MirrorError):
                    mirror.name_and_version(filename)

    def test_normalize(self):
        self.assertEqual(mirror.normalize("Zope.Interface__x"), "zope-interface-x")


class FakeRegistry(Workspace):
    """A running FakeGitLab, with the CI variables pointing at it."""

    def setUp(self):
        super().setUp()
        FakeGitLab.files = {}
        self.server = HTTPServer(("127.0.0.1", 0), FakeGitLab)
        self.server.forward = False
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        env = {
            "CI_API_V4_URL": f"http://127.0.0.1:{self.server.server_port}/api/v4",
            "CI_PROJECT_ID": "42",
            "CI_JOB_TOKEN": TOKEN,
        }
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)


class RegistryTests(FakeRegistry):
    def run_cli(self, *argv):
        out = io.StringIO()
        with redirect_stdout(out):
            code = mirror.main(list(argv))
        return code, out.getvalue()

    def test_publish_uploads_only_what_is_missing_and_a_rerun_uploads_nothing(self):
        linux = self.target("linux-py3.12")
        windows = self.target("windows-py3.12")
        make_wheel(linux, "requests-2.32.5-py3-none-any.whl", ">=3.9")
        make_wheel(linux, "PyYAML-6.0.3-cp312-cp312-manylinux_2_28_x86_64.whl")
        # the same pure wheel in two targets is one upload
        make_wheel(windows, "requests-2.32.5-py3-none-any.whl", ">=3.9")
        make_wheel(windows, "pywin32-312-cp312-cp312-win_amd64.whl")
        # already in the registry before this run
        FakeGitLab.files["pywin32"] = {"pywin32-312-cp312-cp312-win_amd64.whl": {}}

        code, out = self.run_cli("publish")
        self.assertEqual(code, 0, out)
        self.assertIn("3 file(s): 2 uploaded, 1 already in the registry", out)
        uploaded = FakeGitLab.files["requests"]["requests-2.32.5-py3-none-any.whl"]
        self.assertEqual(uploaded["name"], "requests")
        self.assertEqual(uploaded["version"], "2.32.5")
        self.assertEqual(uploaded["requires_python"], ">=3.9")
        self.assertEqual(len(uploaded["sha256_digest"]), 64)
        self.assertIn("pyyaml", FakeGitLab.files)

        code, out = self.run_cli("publish")
        self.assertEqual(code, 0, out)
        self.assertIn("0 uploaded, 3 already in the registry", out)

    def test_a_forwarded_lookup_is_not_read_as_the_registrys_own_files(self):
        """GitLab 302s an unknown name to pypi.org. Following it made every
        file look published and the first real run uploaded nothing."""
        self.server.forward = True
        make_wheel(self.target("linux-py3.12"), "idna-3.20-py3-none-any.whl")
        code, out = self.run_cli("publish")
        self.assertEqual(code, 0, out)
        self.assertIn("1 file(s): 1 uploaded, 0 already in the registry", out)

    def test_a_source_distribution_in_a_wheelhouse_is_never_uploaded(self):
        """Air-gapped hosts have no compiler: an sdist in the registry is a
        package pip can find there and cannot install."""
        wheelhouse = self.target("linux-py3.12")
        make_wheel(wheelhouse, "idna-3.20-py3-none-any.whl")
        (wheelhouse / "pkg-0.1.tar.gz").write_bytes(b"not a wheel")
        err = io.StringIO()
        with mock.patch("sys.stderr", err):
            code, out = self.run_cli("publish")
        self.assertEqual(code, 0, out)
        self.assertIn("1 file(s): 1 uploaded", out)
        self.assertNotIn("pkg", FakeGitLab.files)
        self.assertIn("ignoring packages/linux-py3.12/wheelhouse/pkg-0.1.tar.gz", err.getvalue())

    def test_dry_run_uploads_nothing(self):
        make_wheel(self.target("linux-py3.12"), "idna-3.20-py3-none-any.whl")
        code, out = self.run_cli("publish", "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("would upload idna-3.20-py3-none-any.whl", out)
        self.assertEqual(FakeGitLab.files, {})

    def test_a_concurrent_upload_of_the_same_file_is_not_an_error(self):
        path = make_wheel(self.target("linux-py3.12"), "idna-3.20-py3-none-any.whl")
        registry = mirror.Registry.from_env()
        registry.upload(path)
        registry.upload(path)  # the registry answers 400 "already been taken"

    def test_prune_deletes_only_published_files(self):
        wheelhouse = self.target("linux-py3.12")
        published = make_wheel(wheelhouse, "idna-3.20-py3-none-any.whl")
        pending = make_wheel(wheelhouse, "certifi-2026.7.22-py3-none-any.whl")
        FakeGitLab.files["idna"] = {published.name: {}}
        code, out = self.run_cli("prune")
        self.assertEqual(code, 0, out)
        self.assertFalse(published.exists())
        self.assertTrue(pending.exists())

    def test_a_wrong_token_fails_loudly(self):
        make_wheel(self.target("linux-py3.12"), "idna-3.20-py3-none-any.whl")
        with mock.patch.dict(os.environ, {"CI_JOB_TOKEN": "wrong"}):
            with self.assertRaises(mirror.MirrorError):
                mirror.Registry.from_env().existing("idna")

    def test_missing_settings_are_named(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(mirror.MirrorError) as caught:
                mirror.Registry.from_env()
        self.assertIn("PYPI_TOKEN (or CI_JOB_TOKEN)", str(caught.exception))


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(
    all(shutil.which(t) for t in ("curl", "unzip", "sha256sum")), "publish.sh needs curl, unzip, sha256sum"
)
class ShellPublishTests(FakeRegistry):
    """publish.sh, the curl fallback, against the same fake registry."""

    def run_sh(self, *argv):
        env = dict(os.environ, PACKAGES_DIR=str(self.packages), PATH=SHELL_PATH)
        result = subprocess.run(
            ["sh", str(Path(mirror.__file__).with_name("publish.sh")), *argv],
            env=env, capture_output=True, text=True,
        )
        return result.returncode, result.stdout, result.stderr

    def test_uploads_only_what_is_missing_and_a_rerun_uploads_nothing(self):
        linux = self.target("linux-py3.12")
        windows = self.target("windows-py3.12")
        make_wheel(linux, "requests-2.32.5-py3-none-any.whl", "<4,>=3.9")
        make_wheel(windows, "requests-2.32.5-py3-none-any.whl", "<4,>=3.9")
        make_wheel(windows, "pywin32-312-cp312-cp312-win_amd64.whl")
        FakeGitLab.files["pywin32"] = {"pywin32-312-cp312-cp312-win_amd64.whl": {}}
        code, out, err = self.run_sh()
        self.assertEqual(code, 0, err)
        self.assertIn("2 file(s): 1 uploaded, 1 already in the registry", out)
        fields = FakeGitLab.files["requests"]["requests-2.32.5-py3-none-any.whl"]
        # A --form value starting with < would make curl read a file of that name.
        self.assertEqual(fields["requires_python"], "<4,>=3.9")
        self.assertEqual(fields["version"], "2.32.5")
        self.assertEqual(len(fields["sha256_digest"]), 64)
        code, out, err = self.run_sh()
        self.assertIn("0 uploaded, 2 already in the registry", out)

    def test_a_forwarded_lookup_is_not_read_as_the_registrys_own_files(self):
        self.server.forward = True
        make_wheel(self.target("linux-py3.12"), "idna-3.20-py3-none-any.whl")
        code, out, err = self.run_sh()
        self.assertEqual(code, 0, err)
        self.assertIn("1 file(s): 1 uploaded, 0 already in the registry", out)

    def test_a_source_distribution_is_never_uploaded(self):
        wheelhouse = self.target("linux-py3.12")
        (wheelhouse / "pkg-0.1.tar.gz").write_bytes(b"not a wheel")
        code, out, err = self.run_sh()
        self.assertEqual(code, 0, err)
        self.assertIn("0 file(s)", out)
        self.assertIn("ignoring linux-py3.12/wheelhouse/pkg-0.1.tar.gz", err)

    def test_dry_run_uploads_nothing(self):
        make_wheel(self.target("linux-py3.12"), "idna-3.20-py3-none-any.whl")
        code, out, err = self.run_sh("--dry-run")
        self.assertIn("would upload idna-3.20-py3-none-any.whl", out)
        self.assertEqual(FakeGitLab.files, {})

    def test_a_wrong_token_fails(self):
        make_wheel(self.target("linux-py3.12"), "idna-3.20-py3-none-any.whl")
        os.environ["CI_JOB_TOKEN"] = "wrong"
        code, out, err = self.run_sh()
        self.assertNotEqual(code, 0)
        self.assertIn("HTTP 401", err)
