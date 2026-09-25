"""Tests for mirror.py. Standard library only: python3 -m unittest discover -s tests

The registry tests run against a small fake of GitLab's PyPI endpoints, so the
skip-existing logic is exercised end to end without a GitLab instance.
"""

import base64
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import unittest
import urllib.parse
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
        header = self.headers.get("Authorization", "")
        password = base64.b64decode(header[6:]).decode().split(":", 1)[-1] if header.startswith("Basic ") else ""
        if password != TOKEN:
            self.send_response(401)
            self.end_headers()
            return False
        return True

    def do_GET(self):
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
            body = [{"id": i, "name": n} for i, n in enumerate(names)]
        self.send_response(200)
        self.end_headers()
        self.wfile.write(json.dumps(body).encode())

    def do_POST(self):
        if self.path == "/contentListener":  # stands in for NiFi's ListenHTTP
            body = self.rfile.read(int(self.headers["Content-Length"]))
            self.server.posted.append((self.headers["filename"], self.headers["x-sha256"], body))
            self.send_response(self.server.post_status)
            self.end_headers()
            return
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


class Workspace(unittest.TestCase):
    """A throwaway packages/ tree with mirror.ROOT pointed at it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        # The flags come from the environment at import; a pipeline variable must not leak in.
        patcher = mock.patch.multiple(mirror, ROOT=root, PACKAGES=root / "packages",
                                      BUNDLE_REQUIREMENTS=True, BUNDLE_GIT=False)
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
        self.server.posted, self.server.post_status = [], 200
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
        self.assertEqual(len(list(linux.iterdir())), 3)  # the shared wheel is on disk once
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
        self.assertIn("ignoring wheelhouse/pkg-0.1.tar.gz", err.getvalue())

    def test_a_git_lfs_pointer_is_named_as_one(self):
        """A runner without git-lfs checks out pointer files under the wheels' names."""
        (self.target("linux-py3.12") / "idna-3.20-py3-none-any.whl").write_text(
            "version https://git-lfs.github.com/spec/v1\noid sha256:" + "0" * 64 + "\nsize 70000\n")
        err = io.StringIO()
        with mock.patch("sys.stderr", err):
            code, _ = self.run_cli("publish")
        self.assertEqual(code, 1)
        self.assertIn("a Git LFS pointer, not a wheel", err.getvalue())
        self.assertEqual(FakeGitLab.files, {})

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

    def test_wheels_in_per_target_wheelhouses_move_into_the_shared_one_once(self):
        for name in ("linux-py3.12", "windows-py3.12"):
            self.target(name)
            old = self.packages / name / "wheelhouse"
            old.mkdir()
            make_wheel(old, "requests-2.32.5-py3-none-any.whl")
        make_wheel(self.packages / "windows-py3.12" / "wheelhouse", "pywin32-312-cp312-cp312-win_amd64.whl")
        code, out = self.run_cli("publish")
        self.assertEqual(code, 0, out)
        self.assertIn("2 file(s): 2 uploaded", out)
        self.assertEqual(sorted(p.name for p in (self.packages.parent / "wheelhouse").iterdir()),
                         ["pywin32-312-cp312-cp312-win_amd64.whl", "requests-2.32.5-py3-none-any.whl"])
        self.assertEqual(list(self.packages.glob("*/wheelhouse")), [])

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


class BundleTests(FakeRegistry):
    def run_cli(self, *argv):
        out = io.StringIO()
        with redirect_stdout(out):
            code = mirror.main(list(argv))
        return code, out.getvalue()

    def bundles(self, directory):
        return sorted(Path(directory).glob("pypi-*.tar"))

    def test_publish_bundles_only_what_it_uploaded(self):
        wheelhouse = self.target("linux-py3.12")
        make_wheel(wheelhouse, "idna-3.20-py3-none-any.whl")
        FakeGitLab.files["idna"] = {"idna-3.20-py3-none-any.whl": {}}      # already mirrored and shipped
        make_wheel(wheelhouse, "certifi-2026.7.22-py3-none-any.whl")        # new since the last run
        out_dir = Path(self.tmp.name) / "delta"
        code, out = self.run_cli("publish", "--bundle", str(out_dir))
        self.assertEqual(code, 0, out)
        [bundle] = self.bundles(out_dir)
        with tarfile.open(bundle) as tar:
            self.assertEqual(sorted(tar.getnames()), ["MANIFEST.json", "requirements/linux-py3.12/requirements.txt",
                                                      "wheels/certifi-2026.7.22-py3-none-any.whl"])
        sidecar = bundle.with_name(bundle.name + ".sha256").read_text().split()[0]
        self.assertEqual(sidecar, mirror.sha256_file(bundle))
        code, out = self.run_cli("publish", "--bundle", str(out_dir))   # nothing new: no second bundle
        self.assertIn("no bundle written", out)
        self.assertEqual(len(self.bundles(out_dir)), 1)

    def test_import_uploads_a_bundle_into_an_empty_registry_once(self):
        make_wheel(self.target("linux-py3.12", "certifi\n"), "certifi-2026.7.22-py3-none-any.whl", ">=3.7")
        out_dir = Path(self.tmp.name) / "delta"
        self.run_cli("publish", "--bundle", str(out_dir))
        FakeGitLab.files = {}                                            # the high side starts empty
        [bundle] = self.bundles(out_dir)
        code, out = self.run_cli("import", str(bundle))
        self.assertEqual(code, 0, out)
        self.assertIn("1 file(s): 1 uploaded", out)
        self.assertEqual(FakeGitLab.files["certifi"]["certifi-2026.7.22-py3-none-any.whl"]["requires_python"], ">=3.7")
        code, out = self.run_cli("import", str(bundle))
        self.assertIn("0 uploaded, 1 already in the registry", out)

    def test_import_writes_the_requirements_that_came_with_the_bundle(self):
        make_wheel(self.target("linux-py3.12", "certifi\n"), "certifi-2026.7.22-py3-none-any.whl")
        (self.packages / "linux-py3.12" / "platforms.txt").write_text("manylinux_2_28_x86_64\n")
        out_dir = Path(self.tmp.name) / "delta"
        self.run_cli("publish", "--bundle", str(out_dir))
        [bundle] = self.bundles(out_dir)
        high = Path(self.tmp.name) / "high"
        code, out = self.run_cli("import", str(bundle), "--requirements", str(high))
        self.assertEqual(code, 0, out)
        self.assertEqual((high / "linux-py3.12" / "requirements.txt").read_text(), "certifi\n")
        self.assertEqual((high / "linux-py3.12" / "platforms.txt").read_text(), "manylinux_2_28_x86_64\n")

    def test_a_tampered_bundle_is_refused(self):
        make_wheel(self.target("linux-py3.12"), "certifi-2026.7.22-py3-none-any.whl")
        out_dir = Path(self.tmp.name) / "delta"
        self.run_cli("publish", "--bundle", str(out_dir))
        [bundle] = self.bundles(out_dir)
        with open(bundle, "ab") as handle:
            handle.write(b"x")
        FakeGitLab.files = {}
        err = io.StringIO()
        with mock.patch("sys.stderr", err):
            code, _ = self.run_cli("import", str(bundle))
        self.assertEqual(code, 1)
        self.assertIn("checksum does not match", err.getvalue())
        self.assertEqual(FakeGitLab.files, {})

    def test_a_requirements_change_alone_ships_a_bundle(self):
        make_wheel(self.target("linux-py3.12", "certifi\n"), "certifi-2026.7.22-py3-none-any.whl")
        out_dir = Path(self.tmp.name) / "delta"
        self.run_cli("publish", "--bundle", str(out_dir))
        (self.packages / "linux-py3.12" / "requirements.txt").write_text("")    # a package dropped, nothing new
        code, out = self.run_cli("publish", "--bundle", str(out_dir))
        self.assertEqual(code, 0, out)
        self.assertIn("(0 file(s)", out)
        self.assertEqual(len(self.bundles(out_dir)), 2)

    def test_with_every_extra_off_a_run_with_nothing_new_writes_nothing(self):
        self.target("linux-py3.12", "certifi\n")
        out_dir = Path(self.tmp.name) / "delta"
        with mock.patch.object(mirror, "BUNDLE_REQUIREMENTS", False):
            code, out = self.run_cli("publish", "--bundle", str(out_dir))
        self.assertIn("no bundle written", out)
        self.assertEqual(self.bundles(out_dir), [])

    @unittest.skipUnless(shutil.which("git"), "needs git")
    def test_git_history_travels_in_the_bundle_and_fetches_on_the_high_side(self):
        root = self.packages.parent
        make_wheel(self.target("linux-py3.12", "certifi\n"), "certifi-2026.7.22-py3-none-any.whl")

        def git(*args, cwd=root):
            return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
                                  cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()
        git("init", "-q")
        git("add", "packages")
        git("commit", "-qm", "first")
        out_dir = Path(self.tmp.name) / "delta"
        with mock.patch.object(mirror, "BUNDLE_GIT", True):
            self.run_cli("publish", "--bundle", str(out_dir))
            code, out = self.run_cli("publish", "--bundle", str(out_dir))       # same commit: nothing new
            self.assertIn("no bundle written", out)
            git("commit", "-q", "--allow-empty", "-m", "second")
            code, out = self.run_cli("publish", "--bundle", str(out_dir))       # new commit, no new wheels
        self.assertEqual(code, 0, out)
        first, second = self.bundles(out_dir)
        FakeGitLab.files = {}
        repo = Path(self.tmp.name) / "repo.bundle"
        code, out = self.run_cli("import", str(first), str(second), "--git-bundle", str(repo))
        self.assertEqual(code, 0, out)
        high = Path(self.tmp.name) / "high"
        high.mkdir()
        git("init", "-q", cwd=high)
        git("fetch", "-q", str(repo), "HEAD:refs/heads/low-side", cwd=high)
        self.assertEqual(git("rev-parse", "low-side", cwd=high), git("rev-parse", "HEAD"))
        self.assertEqual(git("log", "--format=%s", "low-side", cwd=high).splitlines(), ["second", "first"])

    def test_a_bundle_is_posted_whole_and_a_failed_post_is_retried_next_run(self):
        make_wheel(self.target("linux-py3.12", "certifi\n"), "certifi-2026.7.22-py3-none-any.whl")
        out_dir = Path(self.tmp.name) / "delta"
        url = f"http://127.0.0.1:{self.server.server_port}/contentListener"
        self.server.post_status = 503                                          # NiFi down
        err = io.StringIO()
        with mock.patch("sys.stderr", err):
            code, _ = self.run_cli("publish", "--bundle", str(out_dir), "--post", url)
        self.assertEqual(code, 1)
        self.assertIn("HTTP 503", err.getvalue())
        self.server.post_status = 200
        code, out = self.run_cli("publish", "--bundle", str(out_dir), "--post", url)   # nothing new to upload
        self.assertEqual(code, 0, out)
        [bundle] = self.bundles(out_dir)[-1:]
        name, sha256, body = self.server.posted[-1]
        self.assertEqual((name, sha256, body), (bundle.name, mirror.sha256_file(bundle), bundle.read_bytes()))

    def test_inbox_imports_oldest_first_and_moves_each_bundle_to_done(self):
        make_wheel(self.target("linux-py3.12", "certifi\n"), "certifi-2026.7.22-py3-none-any.whl")
        inbox = Path(self.tmp.name) / "inbox"
        self.run_cli("publish", "--bundle", str(inbox))
        (self.packages / "linux-py3.12" / "requirements.txt").write_text("certifi\nidna\n")
        make_wheel(self.packages.parent / "wheelhouse", "idna-3.20-py3-none-any.whl")
        self.run_cli("publish", "--bundle", str(inbox))
        FakeGitLab.files = {}
        high = Path(self.tmp.name) / "high"
        code, out = self.run_cli("import", "--inbox", str(inbox), "--requirements", str(high))
        self.assertEqual(code, 0, out)
        self.assertIn("2 file(s): 2 uploaded", out)
        self.assertEqual((high / "linux-py3.12" / "requirements.txt").read_text(), "certifi\nidna\n")  # newest won
        self.assertEqual(list(inbox.glob("pypi-*")), [])
        self.assertEqual(len(list((inbox / "done").glob("pypi-*.tar"))), 2)
        code, out = self.run_cli("import", "--inbox", str(inbox))                    # empty inbox is not an error
        self.assertEqual(code, 0, out)
        self.assertIn("0 file(s)", out)

    def test_export_names_the_token_it_needs_when_given_a_job_token(self):
        err = io.StringIO()
        with mock.patch("sys.stderr", err):
            code, _ = self.run_cli("export", "--since", "2026-09-15")
        self.assertEqual(code, 1)
        self.assertIn("set PYPI_TOKEN holding a project access token", err.getvalue())

    def test_export_in_ci_names_the_ci_variable(self):
        os.environ["GITLAB_CI"] = "true"
        err = io.StringIO()
        with mock.patch("sys.stderr", err):
            code, _ = self.run_cli("export", "--since", "2026-09-15")
        self.assertEqual(code, 1)
        self.assertIn("add a masked CI/CD variable EXPORT_TOKEN", err.getvalue())

    def test_export_rebuilds_a_bundle_of_files_received_since_a_date(self):
        os.environ["PYPI_TOKEN"], os.environ["PYPI_USERNAME"] = TOKEN, "gitlab-ci-token-as-pat"
        wheelhouse = self.target("linux-py3.12")
        self.server.now = "2026-09-01T10:00:00Z"
        make_wheel(wheelhouse, "idna-3.20-py3-none-any.whl")
        self.run_cli("publish")
        self.server.now = "2026-09-20T10:00:00Z"
        make_wheel(wheelhouse, "certifi-2026.7.22-py3-none-any.whl")
        self.run_cli("publish")
        out_dir = Path(self.tmp.name) / "export"
        code, out = self.run_cli("export", "--since", "2026-09-15", "--bundle", str(out_dir))
        self.assertEqual(code, 0, out)
        [bundle] = self.bundles(out_dir)
        with tarfile.open(bundle) as tar:
            self.assertIn("wheels/certifi-2026.7.22-py3-none-any.whl", tar.getnames())
            self.assertNotIn("wheels/idna-3.20-py3-none-any.whl", tar.getnames())


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(
    all(shutil.which(t) for t in ("curl", "unzip", "sha256sum")), "publish.sh needs curl, unzip, sha256sum"
)
class ShellPublishTests(FakeRegistry):
    """publish.sh, the curl fallback, against the same fake registry."""

    def run_sh(self, *argv):
        env = dict(os.environ, WHEELHOUSE_DIR=str(self.packages.parent / "wheelhouse"), PATH=SHELL_PATH)
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
        self.assertIn("ignoring wheelhouse/pkg-0.1.tar.gz", err)

    def test_a_git_lfs_pointer_is_never_uploaded(self):
        (self.target("linux-py3.12") / "idna-3.20-py3-none-any.whl").write_text(
            "version https://git-lfs.github.com/spec/v1\noid sha256:" + "0" * 64 + "\nsize 70000\n")
        code, out, err = self.run_sh()
        self.assertEqual(code, 1)
        self.assertIn("is a Git LFS pointer", err)
        self.assertEqual(FakeGitLab.files, {})

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
