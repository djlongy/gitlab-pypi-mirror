"""Tests for mirror.py. Standard library only: python3 -m unittest discover -s tests

The registry tests run against a small fake of GitLab's PyPI endpoints, so the
skip-existing logic is exercised end to end without a GitLab instance.
"""

import base64
import fnmatch
import io
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import threading
import unittest
import urllib.parse
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import mirror  # noqa: E402

TOKEN = "t0ken"
UPSTREAM_FILES = ["idna-3.20-py3-none-any.whl", "requests-2.32.5-py3-none-any.whl"]


class FakeGitLab(BaseHTTPRequestHandler):
    """GET .../packages/pypi/simple/<name> and POST .../packages/pypi."""

    files = {}  # normalized name -> {filename: form fields}

    def log_message(self, *args):
        pass

    def _authorized(self):
        header = self.headers.get("Authorization", "")
        password = base64.b64decode(header[6:]).decode().split(":", 1)[-1] if header.startswith("Basic ") else ""
        if password != TOKEN:
            self.send_response(401)
            self.end_headers()
            return False
        return True

    def do_GET(self):
        getattr(self.server, "paths", []).append(self.path)
        if "/packages/generic/" in self.path:
            return self._generic()
        if "/packages?" in self.path or "/package_files" in self.path or "/packages/pypi/files/" in self.path:
            return self._packages_api()
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

    def _packages_api(self):
        """GET /packages, /packages/<id>/package_files and /packages/pypi/files/<sha>/<name>."""
        if "/packages/pypi/files/" not in self.path and self.headers.get("PRIVATE-TOKEN") != TOKEN:
            self.send_response(404)  # the REST API ignores Basic auth and hides the project
            self.end_headers()
            return
        if "/packages/pypi/files/" in self.path and not self._authorized():
            return
        names = sorted(self.files)
        if "/packages/pypi/files/" in self.path:
            filename = urllib.parse.unquote(self.path.rsplit("/", 1)[1])
            data = next((f["_data"] for b in self.files.values() for n, f in b.items() if n == filename), None)
            self.send_response(200 if data is not None else 404)
            self.end_headers()
            self.wfile.write(data or b"")
            return
        if "/package_files" in self.path:
            pid = int(re.search(r"/packages/(\d+)/package_files", self.path)[1])
            body = [{"file_name": n, "file_sha256": f.get("sha256_digest", ""),
                     "created_at": f.get("_created", "2026-09-01T00:00:00Z")}
                    for n, f in self.files[names[pid]].items()]
        else:
            # One package per name here; GitLab has one per name and version.
            body = [{"id": i, "name": n, "version": next((f.get("version", "") for f in self.files[n].values()), "")}
                    for i, n in enumerate(names)]
        self.send_response(200)
        self.end_headers()
        self.wfile.write(json.dumps(body).encode())

    def _generic(self):
        """GET and PUT of a generic package file: the request ledger. A job token arrives as JOB-TOKEN, any
        other token as Basic auth, the one form a deploy token shares with personal and project tokens."""
        if self.headers.get("JOB-TOKEN") != TOKEN and not self._authorized():
            return
        if self.command == "PUT":
            self.server.generic[self.path] = self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(201)
            self.end_headers()
            return
        data = self.server.generic.get(self.path)
        self.send_response(200 if data is not None else 404)
        self.end_headers()
        self.wfile.write(data or b"")

    def do_PUT(self):
        return self._generic()

    def do_POST(self):
        if self.path in ("/contentListener", "/pypi-request"):  # stands in for NiFi's ListenHTTP
            body = self.rfile.read(int(self.headers["Content-Length"]))
            self.server.posted.append((self.headers["filename"], self.headers["x-sha256"], body))
            self.server.post_headers.append(dict(self.headers.items()))  # names as sent
            status = self.server.post_status  # a list answers one POST per entry, in order
            self.send_response(status.pop(0) if isinstance(status, list) else status)
            self.end_headers()
            return
        if not self._authorized():
            return
        length = int(self.headers["Content-Length"])
        raw = self.rfile.read(length)
        refusal = getattr(self.server, "upload_refusal", None)
        if refusal:
            self.send_response(refusal[0])
            self.end_headers()
            self.wfile.write(refusal[1].encode())
            return
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
                fields["_data"] = value
            else:
                fields[name] = value.decode()
        key = mirror.normalize(fields["name"])
        bucket = self.files.setdefault(key, {})
        if filename in bucket:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b'{"message":"Validation failed: File name has already been taken"}')
            return
        fields["_created"] = self.server.now if hasattr(self.server, "now") else "2026-09-01T00:00:00Z"
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


class CliMixin:
    def run_cli(self, *argv):
        """main() with both streams captured: returns (exit code, stdout) and leaves stderr in self.err."""
        out, err = io.TextIOWrapper(io.BytesIO(), encoding="utf-8", write_through=True), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                code = mirror.main(list(argv))
            except SystemExit as stop:
                code = stop.code
        self.err = err.getvalue()
        self.out_bytes = out.buffer.getvalue()
        return code, self.out_bytes.decode("utf-8", "replace")


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

    def target(self, name: str, requirements: str = "") -> Path:
        """Create packages/<name>/ and return the shared wheelhouse every target downloads into."""
        (self.packages / name).mkdir(exist_ok=True)
        (self.packages / name / "requirements.txt").write_text(requirements)
        path = self.packages.parent / "wheelhouse"
        path.mkdir(exist_ok=True)
        return path


class TargetTests(CliMixin, Workspace):
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

    def test_pip_judges_environment_markers_as_the_target(self):
        """A Linux host downloading for Windows must not pull Linux-only dependencies."""
        (self.packages / "windows-py3.10").mkdir()
        target = mirror.Target(self.packages / "windows-py3.10")
        script = ("from pip._vendor.packaging.markers import Marker\n"
                  "print(Marker('platform_system == \"Linux\"').evaluate(), "
                  "Marker('sys_platform == \"win32\" and python_version == \"3.10\"').evaluate())\n")
        stub = mirror.PIP_AS_TARGET.replace('runpy.run_module("pip", run_name="__main__", alter_sys=True)', script)
        result = subprocess.run([sys.executable, "-c", stub, mirror.pip_as(target)[3]],
                                capture_output=True, text=True, check=True)
        self.assertEqual(result.stdout.split(), ["False", "True"])

    def test_a_misnamed_directory_is_refused(self):
        for bad in ("py3.12-linux", "linux-3.12", "macos-py3.12", "linux-py3.12-sparc"):
            with self.subTest(bad):
                path = self.packages / bad
                path.mkdir()
                with self.assertRaises(mirror.MirrorError):
                    mirror.find_targets()
                path.rmdir()


def renovate_gaps(root: Path) -> list:
    """Targets under root/packages that renovate.json does not hold to their Python version."""
    config = json.loads((root / "renovate.json").read_text())
    gaps = [] if config.get("constraintsFiltering") == "strict" else ['set "constraintsFiltering": "strict"']
    for target in sorted(p for p in (root / "packages").iterdir() if p.is_dir()):
        python = mirror.Target(target).python
        file = f"packages/{target.name}/requirements.txt"
        pythons = sorted({rule["constraints"]["python"] for rule in config.get("packageRules", [])
                          if "python" in rule.get("constraints", {})
                          and any(fnmatch.fnmatchcase(file, glob) for glob in rule.get("matchFileNames", []))})
        if pythons != [python]:
            gaps.append(f"packages/{target.name}: renovate.json gives Python {', '.join(pythons) or 'nothing'}, "
                        f"expected {python}; add a packageRule matching packages/*-py{python}/** with "
                        f'"constraints": {{"python": "{python}"}}')
    return gaps


class RenovateTests(unittest.TestCase):
    def test_renovate_json_holds_every_target_to_its_python(self):
        self.assertEqual(renovate_gaps(Path(mirror.__file__).resolve().parent), [])

    def test_a_target_without_a_rule_is_a_gap(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ("linux-py3.9", "linux-py3.9-aarch64", "windows-py3.12"):
                (root / "packages" / name).mkdir(parents=True)
            rule = {"matchFileNames": ["packages/*-py3.9/**", "packages/*-py3.9-*/**"], "constraints": {"python": "3.9"}}
            (root / "renovate.json").write_text(json.dumps({"constraintsFiltering": "strict", "packageRules": [rule]}))
            with mock.patch.object(mirror, "ROOT", root):
                self.assertEqual([g.split(":")[0] for g in renovate_gaps(root)], ["packages/windows-py3.12"])
                (root / "renovate.json").write_text(json.dumps({"packageRules": [rule]}))
                self.assertIn("constraintsFiltering", renovate_gaps(root)[0])


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

    def test_a_source_distribution_names_its_project_and_version(self):
        for filename, expected in (("python-dateutil-2.8.2.tar.gz", ("python-dateutil", "2.8.2")),
                                   ("pkg-0.1.zip", ("pkg", "0.1")), ("pyreadline-2.1.zip", ("pyreadline", "2.1"))):
            with self.subTest(filename):
                self.assertEqual(mirror.name_and_version(filename), expected)
        for filename in ("pkg-0.1-py3.whl", "pkg.tar.gz", "pkg-0.1.tar.bz2", "../x-1.0.zip"):
            with self.subTest(filename):
                with self.assertRaises(mirror.MirrorError):
                    mirror.name_and_version(filename)

    def test_sdists_are_published_only_when_asked(self):
        for flag, expected in (("", False), ("false", False), ("true", True)):
            with self.subTest(flag=flag), mock.patch.dict(os.environ, {"MIRROR_PUBLISH_SDISTS": flag}):
                self.assertEqual(mirror.published("pycrc16-0.1.2.tar.gz"), expected)
                self.assertTrue(mirror.published("six-1.17.0-py2.py3-none-any.whl"))
                self.assertFalse(mirror.published("pkg-0.1.tar.bz2"))

    def test_normalize(self):
        self.assertEqual(mirror.normalize("Zope.Interface__x"), "zope-interface-x")


class FakeRegistry(Workspace):
    """A running FakeGitLab, with the CI variables pointing at it."""

    def setUp(self):
        super().setUp()
        FakeGitLab.files = {}
        self.server = HTTPServer(("127.0.0.1", 0), FakeGitLab)
        self.server.forward = False
        self.server.posted, self.server.post_status, self.server.post_headers = [], 200, []
        self.server.paths, self.server.generic = [], {}
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


class RegistryTests(CliMixin, FakeRegistry):

    def test_the_gitlab_ledger_takes_a_deploy_token(self):
        """A deploy token works only as Basic auth on generic packages, never as PRIVATE-TOKEN."""
        with mock.patch.dict(os.environ, {"MIRROR_LEDGER": "gitlab", "MIRROR_TOKEN": TOKEN,
                                          "MIRROR_USERNAME": "gitlab+deploy-token-1"}):
            ledger = mirror.Ledger(mirror.Registry.from_env())
            ledger.save({"six-1.17.0-py2.py3-none-any.whl": {"sha256": "0"}})
            self.assertEqual(list(ledger.load()), ["six-1.17.0-py2.py3-none-any.whl"])

    def test_sync_uploads_only_what_is_missing_and_a_rerun_uploads_nothing(self):
        linux = self.target("linux-py3.12")
        windows = self.target("windows-py3.12")
        make_wheel(linux, "requests-2.32.5-py3-none-any.whl", ">=3.9")
        make_wheel(linux, "PyYAML-6.0.3-cp312-cp312-manylinux_2_28_x86_64.whl")
        # the same pure wheel in two targets is one upload
        make_wheel(windows, "requests-2.32.5-py3-none-any.whl", ">=3.9")
        make_wheel(windows, "pywin32-312-cp312-cp312-win_amd64.whl")
        # already in the registry before this run
        FakeGitLab.files["pywin32"] = {"pywin32-312-cp312-cp312-win_amd64.whl": {}}

        code, out = self.run_cli("sync")
        self.assertEqual(code, 0, out)
        self.assertIn("3 file(s): 2 uploaded, 1 already in the registry", self.err)
        self.assertEqual(len(list(linux.iterdir())), 3)  # the shared wheel is on disk once
        uploaded = FakeGitLab.files["requests"]["requests-2.32.5-py3-none-any.whl"]
        self.assertEqual(uploaded["name"], "requests")
        self.assertEqual(uploaded["version"], "2.32.5")
        self.assertEqual(uploaded["requires_python"], ">=3.9")
        self.assertEqual(len(uploaded["sha256_digest"]), 64)
        self.assertIn("pyyaml", FakeGitLab.files)

        code, out = self.run_cli("sync")
        self.assertEqual(code, 0, out)
        self.assertIn("0 uploaded, 3 already in the registry", self.err)

    def test_a_forwarded_lookup_is_not_read_as_the_registrys_own_files(self):
        """GitLab 302s an unknown name to pypi.org. Following it made every
        file look published and the first real run uploaded nothing."""
        self.server.forward = True
        make_wheel(self.target("linux-py3.12"), "idna-3.20-py3-none-any.whl")
        code, out = self.run_cli("sync")
        self.assertEqual(code, 0, out)
        self.assertIn("1 file(s): 1 uploaded, 0 already in the registry", self.err)

    def test_a_source_distribution_in_a_wheelhouse_is_never_uploaded(self):
        """Air-gapped hosts have no compiler: an sdist in the registry is a
        package pip can find there and cannot install."""
        wheelhouse = self.target("linux-py3.12")
        make_wheel(wheelhouse, "idna-3.20-py3-none-any.whl")
        (wheelhouse / "pkg-0.1.tar.gz").write_bytes(b"not a wheel")
        code, out = self.run_cli("sync")
        self.assertEqual(code, 0, out)
        self.assertIn("1 file(s): 1 uploaded", self.err)
        self.assertNotIn("pkg", FakeGitLab.files)
        self.assertIn("ignoring wheelhouse/pkg-0.1.tar.gz", self.err)

    def test_a_git_lfs_pointer_is_named_as_one(self):
        """A runner without git-lfs checks out pointer files under the wheels' names."""
        (self.target("linux-py3.12") / "idna-3.20-py3-none-any.whl").write_text(
            "version https://git-lfs.github.com/spec/v1\noid sha256:" + "0" * 64 + "\nsize 70000\n")
        code, _ = self.run_cli("sync")
        self.assertEqual(code, 1)
        self.assertIn("a Git LFS pointer, not a wheel", self.err)
        self.assertEqual(FakeGitLab.files, {})

    def test_dry_run_uploads_nothing(self):
        make_wheel(self.target("linux-py3.12"), "idna-3.20-py3-none-any.whl")
        code, out = self.run_cli("sync", "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("would upload idna-3.20-py3-none-any.whl", out)
        self.assertEqual(FakeGitLab.files, {})

    def test_a_concurrent_upload_of_the_same_file_is_not_an_error(self):
        path = make_wheel(self.target("linux-py3.12"), "idna-3.20-py3-none-any.whl")
        registry = mirror.Registry.from_env()
        registry.upload(path)
        registry.upload(path)  # the registry answers 400 "already been taken"

    def test_a_wrong_token_fails_loudly(self):
        make_wheel(self.target("linux-py3.12"), "idna-3.20-py3-none-any.whl")
        with mock.patch.dict(os.environ, {"CI_JOB_TOKEN": "wrong"}):
            with self.assertRaises(mirror.MirrorError):
                mirror.Registry.from_env().existing("idna")

    def test_missing_settings_are_named(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(mirror.MirrorError) as caught:
                mirror.Registry.from_env()
        self.assertIn("MIRROR_TOKEN (or CI_JOB_TOKEN)", str(caught.exception))

    def test_a_wheelhouse_elsewhere_and_the_committed_one_are_both_published(self):
        """A shell runner keeps downloads on a large disk; a wheel pushed to wheelhouse/ still goes."""
        committed = self.target("linux-py3.12")
        make_wheel(committed, "idna-3.20-py3-none-any.whl")
        elsewhere = Path(self.tmp.name) / "big-disk"
        elsewhere.mkdir()
        make_wheel(elsewhere, "six-1.17.0-py2.py3-none-any.whl")
        make_wheel(elsewhere, "idna-3.20-py3-none-any.whl")
        with mock.patch.dict(os.environ, {"MIRROR_WHEELHOUSE_DIR": str(elsewhere)}):
            code, out = self.run_cli("sync")
        self.assertEqual(code, 0, out)
        self.assertIn("2 file(s): 2 uploaded", self.err)

    def test_sync_stops_on_a_misnamed_target_before_any_upload(self):
        make_wheel(self.target("linux-py3.12"), "idna-3.20-py3-none-any.whl")
        (self.packages / "py3.12-linux").mkdir()
        code, _ = self.run_cli("sync")
        self.assertEqual(code, 1)
        self.assertIn("py3.12-linux: a target directory is named", self.err)
        self.assertEqual(FakeGitLab.files, {})

    def test_sync_stages_builds_under_the_staging_directory_and_removes_its_own(self):
        self.target("linux-py3.12", "idna\n")
        staging = Path(self.tmp.name) / "staging"
        seen = []

        def pip(args, stop_on=None, target=None):
            seen.append(mirror.scratch())
            return 0, []

        for argv, env in ((["--staging-dir", str(staging)], {}), ([], {"MIRROR_STAGING_DIR": str(staging)})):
            with self.subTest(argv=argv), mock.patch.object(mirror, "run_pip", side_effect=pip), \
                    mock.patch.dict(os.environ, env):
                self.assertEqual(self.run_cli("sync", *argv)[0], 0, self.err)
                self.assertEqual(seen[-1].parent, staging)
                self.assertEqual(list(staging.iterdir()), [], "the run's own directory is removed")
        with mock.patch.object(mirror, "run_pip", side_effect=pip):
            self.run_cli("sync")
        self.assertNotEqual(seen[-1].parent, staging)
        self.assertIsNone(mirror.STAGING)


class BundleTests(CliMixin, FakeRegistry):

    def bundles(self, directory):
        return sorted(Path(directory).glob("pypi-*.tar"))

    def bundle(self, *names):
        """A tar as export writes it, holding these wheels."""
        source = Path(self.tmp.name) / "source"
        source.mkdir(exist_ok=True)
        with redirect_stderr(io.StringIO()):
            return mirror.write_bundle([make_wheel(source, n, ">=3.7") for n in names], Path(self.tmp.name) / "carry", "pick")

    def test_import_uploads_a_bundle_into_an_empty_registry_once(self):
        bundle = self.bundle("certifi-2026.7.22-py3-none-any.whl")
        code, out = self.run_cli("import", str(bundle))
        self.assertEqual(code, 0, out)
        self.assertIn("1 file(s): 1 uploaded", self.err)
        self.assertEqual(FakeGitLab.files["certifi"]["certifi-2026.7.22-py3-none-any.whl"]["requires_python"], ">=3.7")
        code, out = self.run_cli("import", str(bundle))
        self.assertIn("0 uploaded, 1 already in the registry", self.err)

    def test_a_tampered_bundle_is_refused(self):
        bundle = self.bundle("certifi-2026.7.22-py3-none-any.whl")
        with open(bundle, "ab") as handle:
            handle.write(b"x")
        code, _ = self.run_cli("import", str(bundle))
        self.assertEqual(code, 1)
        self.assertIn("checksum does not match", self.err)
        self.assertEqual(FakeGitLab.files, {})

    def test_export_reads_its_date_from_export_since(self):
        os.environ["MIRROR_TOKEN"], os.environ["MIRROR_USERNAME"] = TOKEN, "gitlab-ci-token-as-pat"
        self.server.now = "2026-09-20T10:00:00Z"
        make_wheel(self.target("linux-py3.12"), "certifi-2026.7.22-py3-none-any.whl")
        self.run_cli("sync")
        out_dir = Path(self.tmp.name) / "export"
        with mock.patch.dict(os.environ, {"MIRROR_EXPORT_SINCE": "2026-09-15"}):
            code, out = self.run_cli("export", "-t", str(out_dir))
        self.assertEqual(code, 0, out)
        self.assertEqual([b.name.split("-")[1:3] for b in self.bundles(out_dir)], [["since", "2026"]])

    def test_export_names_the_token_it_needs_when_given_a_job_token(self):
        code, _ = self.run_cli("export", "--since", "2026-09-15")
        self.assertEqual(code, 1)
        self.assertIn("set MIRROR_TOKEN holding a project access token", self.err)

    def test_export_in_ci_names_the_ci_variable(self):
        os.environ["GITLAB_CI"] = "true"
        code, _ = self.run_cli("export", "--since", "2026-09-15")
        self.assertEqual(code, 1)
        self.assertIn("add a masked CI/CD variable EXPORT_TOKEN", self.err)

    def test_export_rebuilds_a_bundle_of_files_received_since_a_date(self):
        os.environ["MIRROR_TOKEN"], os.environ["MIRROR_USERNAME"] = TOKEN, "gitlab-ci-token-as-pat"
        wheelhouse = self.target("linux-py3.12")
        self.server.now = "2026-09-01T10:00:00Z"
        make_wheel(wheelhouse, "idna-3.20-py3-none-any.whl")
        self.run_cli("sync")
        self.server.now = "2026-09-20T10:00:00Z"
        make_wheel(wheelhouse, "certifi-2026.7.22-py3-none-any.whl")
        self.run_cli("sync")
        out_dir = Path(self.tmp.name) / "export"
        code, out = self.run_cli("export", "--since", "2026-09-15", "-t", str(out_dir))
        self.assertEqual(code, 0, out)
        [bundle] = self.bundles(out_dir)
        with tarfile.open(bundle) as tar:
            self.assertIn("wheels/certifi-2026.7.22-py3-none-any.whl", tar.getnames())
            self.assertNotIn("wheels/idna-3.20-py3-none-any.whl", tar.getnames())


    def test_export_one_package_for_one_python_version(self):
        os.environ["MIRROR_TOKEN"], os.environ["MIRROR_USERNAME"] = TOKEN, "gitlab-ci-token-as-pat"
        wheelhouse = self.target("linux-py3.12")
        self.target("windows-py3.12")
        for name in ("numpy-2.3.4-cp312-cp312-manylinux_2_27_x86_64.manylinux_2_28_x86_64.whl",
                     "numpy-2.3.4-cp312-cp312-win_amd64.whl", "numpy-2.3.4-cp311-cp311-win_amd64.whl",
                     "idna-3.20-py3-none-any.whl"):
            make_wheel(wheelhouse, name)
        self.run_cli("sync")
        out_dir = Path(self.tmp.name) / "pick"
        code, out = self.run_cli("export", "numpy==2.3.4", "--target", "windows-py3.12",
                                 "-t", str(out_dir))
        self.assertEqual(code, 0, out)
        [bundle] = self.bundles(out_dir)
        with tarfile.open(bundle) as tar:
            self.assertEqual(sorted(n for n in tar.getnames() if n.startswith("wheels/")),
                             ["wheels/numpy-2.3.4-cp312-cp312-win_amd64.whl"])
        code, _ = self.run_cli("export", "numpy==9.9", "-t", str(out_dir))
        self.assertEqual(code, 1)
        self.assertIn("holds no wheel of numpy==9.9", self.err)

    def test_wheel_fits_reads_python_abi3_and_platform_tags(self):
        (self.packages / "linux-py3.12").mkdir()
        [linux] = mirror.find_targets()
        fits = lambda name: mirror.wheel_fits(name, linux)  # noqa: E731
        self.assertTrue(fits("idna-3.20-py3-none-any.whl"))
        self.assertTrue(fits("cryptography-46.0.1-cp311-abi3-manylinux_2_28_x86_64.whl"))
        self.assertFalse(fits("cryptography-46.0.1-cp313-abi3-manylinux_2_28_x86_64.whl"))
        self.assertFalse(fits("numpy-2.3.4-cp312-cp312-win_amd64.whl"))
        self.assertFalse(fits("numpy-2.3.4-cp311-cp311-manylinux_2_28_x86_64.whl"))
        self.assertFalse(fits("numpy-2.3.4-cp312-cp312t-manylinux_2_28_x86_64.whl"))  # free-threaded
        self.assertTrue(fits("tool-1.0-cp312-cp312-linux_x86_64.whl"))
        self.assertEqual(mirror.canonical_version("2.3.4.0"), mirror.canonical_version("2.3.04"))
        self.assertNotEqual(mirror.canonical_version("2.3.4"), mirror.canonical_version("2.3.4.post1"))


class HardeningTests(unittest.TestCase):
    """What a hostile wheel, bundle or endpoint must not get from the mirror."""

    def test_a_token_never_goes_over_plain_http_except_to_this_machine(self):
        with self.assertRaisesRegex(mirror.MirrorError, "over http"):
            mirror.Registry("http://gitlab.example.com/api/v4", "1", "__token__", "t", None)
        mirror.Registry("http://127.0.0.1:1/api/v4", "1", "__token__", "t", None)
        with mock.patch.dict(os.environ, {"MIRROR_REQUEST_TOKEN": "t"}), \
                self.assertRaisesRegex(mirror.MirrorError, "over http"):
            mirror.post_request({"registry": "r", "files": []}, "http://nifi.example.com/pypi-request")

    def test_a_build_sees_no_token_of_the_job(self):
        secret = {"MIRROR_TOKEN": "t", "CI_JOB_TOKEN": "t", "CI_JOB_JWT_V2": "t", "MIRROR_REQUEST_TOKEN": "t",
                  "AWS_SECRET_ACCESS_KEY": "t", "VAULT_ADDR": "t", "SSH_PRIVATE_KEY": "t", "KEEP_ME": "yes"}
        with mock.patch.dict(os.environ, secret):
            code, out = mirror.run_streamed([sys.executable, "-c", "import os; print(sorted(os.environ))"])
        self.assertEqual(code, 0)
        names = " ".join(out)
        self.assertIn("KEEP_ME", names)
        for name in secret.keys() - {"KEEP_ME"}:
            self.assertNotIn(f"'{name}'", names)

    def test_a_huge_metadata_is_refused_before_it_is_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bomb-1.0-py3-none-any.whl"
            with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("bomb-1.0.dist-info/METADATA", b"A" * (mirror.METADATA_LIMIT + 1))
            with self.assertRaisesRegex(mirror.MirrorError, "METADATA is"):
                mirror.read_metadata(path)

    def test_a_manifest_naming_a_file_outside_the_bundle_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            manifest = json.dumps({"files": [{"file": "../../etc/passwd", "sha256": "0" * 64}]}).encode()
            with tarfile.open(tmp / "b.tar", "w") as tar:
                info = tarfile.TarInfo("MANIFEST.json")
                info.size = len(manifest)
                tar.addfile(info, io.BytesIO(manifest))
            (tmp / "out").mkdir()
            with self.assertRaisesRegex(mirror.MirrorError, "refusing manifest entry"):
                mirror.read_bundle(tmp / "b.tar", tmp / "out")


class RequestContractTests(unittest.TestCase):
    """The headers the NiFi processors route on. nifi-python-package-processors tests the same list against a request
    captured from post_request (python/tests/fixtures/gitlab-pypi-mirror-request.json): change both together."""

    HEADERS = ["Content-Type", "Filename", "X-Artifact-Action", "X-Artifact-Format", "X-Artifact-Type", "X-CI-Commit-Sha",
               "X-CI-Pipeline-Url", "X-CI-Project", "X-File-Count", "X-Registry", "X-Sha256"]

    def test_a_request_in_ci_carries_exactly_the_contract_headers(self):
        seen = {}

        class Listener(BaseHTTPRequestHandler):
            def do_POST(self):
                seen.update(self.headers.items())
                seen["body"] = self.rfile.read(int(self.headers["Content-Length"]))
                self.send_response(200)
                self.end_headers()

            def log_message(self, *args):
                pass

        server = HTTPServer(("127.0.0.1", 0), Listener)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        body = {"registry": "https://gitlab.example.com/api/v4/projects/12/packages/pypi", "files": [{"filename": "a"}]}
        ci = {"CI_PROJECT_PATH": "platform/pypi-mirror", "CI_PIPELINE_URL": "https://gitlab.example.com/p/1",
              "CI_COMMIT_SHA": "a" * 40}
        with mock.patch.dict(os.environ, ci):
            mirror.post_request(body, f"http://127.0.0.1:{server.server_port}/pypi-request")
        raw = seen.pop("body")
        sent = {k: v for k, v in seen.items() if k.startswith("X-") or k in ("Filename", "Content-Type")}
        self.assertEqual(sorted(sent), self.HEADERS)
        self.assertEqual((sent["X-Artifact-Type"], sent["X-Registry"], sent["X-File-Count"], sent["X-CI-Project"]),
                         ("python-packages-request", body["registry"], "1", "platform/pypi-mirror"))
        self.assertEqual(sent["X-Sha256"], mirror.hashlib.sha256(raw).hexdigest())


if __name__ == "__main__":
    unittest.main()
