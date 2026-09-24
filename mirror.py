#!/usr/bin/env python3
"""Mirror Python packages into a GitLab PyPI package registry.

Two halves, one file, standard library only:

  On a machine that can reach PyPI (directly or through an HTTP proxy):
      mirror.py download      pip-download every packages/<target>/requirements.txt
      mirror.py prune         delete downloaded files the registry already has

  In GitLab CI, where the runner cannot reach PyPI:
      mirror.py publish       upload every downloaded file the registry lacks

A target is a directory under packages/ named <os>-py<X.Y>[-<arch>]:

  packages/linux-py3.12/requirements.txt
  packages/windows-py3.9/requirements.txt
  packages/linux-py3.11-aarch64/requirements.txt

Files land in packages/<target>/wheelhouse/. Run `mirror.py targets` to see how
each directory name is read.

Python 3.8 or newer.
"""

from __future__ import annotations

import argparse
import base64
import email.parser
import hashlib
import html
import os
import re
import ssl
import subprocess
import sys
import tarfile
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set

ROOT = Path(__file__).resolve().parent
PACKAGES = ROOT / "packages"
WHEELHOUSE = "wheelhouse"

TARGET_RE = re.compile(r"^(?P<os>linux|windows)-py(?P<py>3\.\d{1,2})(?:-(?P<arch>[a-z0-9_]+))?$")
DEFAULT_ARCH = {"linux": "x86_64", "windows": "amd64"}
ARCHES = {"linux": {"x86_64", "aarch64"}, "windows": {"amd64", "arm64", "win32"}}
# Newest glibc the Linux hosts have: 2_28 is RHEL 8 and later. pip does not
# expand a PEP 600 tag to the older ones, so linux_platforms() lists them all.
MANYLINUX = os.environ.get("MANYLINUX", "2_28")
DIST_SUFFIXES = (".whl", ".tar.gz", ".zip")


class MirrorError(Exception):
    pass


# --- targets -----------------------------------------------------------------


class Target:
    def __init__(self, path: Path):
        match = TARGET_RE.match(path.name)
        if not match:
            raise MirrorError(
                f"{path.relative_to(ROOT)}: a target directory is named "
                "<linux|windows>-py<X.Y>[-<arch>], e.g. linux-py3.12 or windows-py3.9-arm64"
            )
        self.path = path
        self.name = path.name
        self.os = match["os"]
        self.python = match["py"]
        self.arch = match["arch"] or DEFAULT_ARCH[self.os]
        if self.arch not in ARCHES[self.os]:
            raise MirrorError(
                f"{self.name}: arch {self.arch!r} is not one of {sorted(ARCHES[self.os])}"
            )

    @property
    def wheelhouse(self) -> Path:
        return self.path / WHEELHOUSE

    @property
    def platforms(self) -> List[str]:
        override = self.path / "platforms.txt"
        if override.is_file():
            return read_lines(override)
        if self.os == "windows":
            return ["win32"] if self.arch == "win32" else [f"win_{self.arch}"]
        return linux_platforms(MANYLINUX, self.arch)

    def files(self) -> List[Path]:
        if not self.wheelhouse.is_dir():
            return []
        return sorted(p for p in self.wheelhouse.iterdir() if p.name.endswith(DIST_SUFFIXES))


def linux_platforms(glibc: str, arch: str) -> List[str]:
    """Every manylinux tag a host with this glibc can install, newest first."""
    match = re.fullmatch(r"2_(\d+)", glibc)
    if not match or int(match[1]) < 17:
        raise MirrorError(f"MANYLINUX={glibc!r}: expected 2_<minor>, minor 17 or newer")
    tags = [f"manylinux_2_{minor}_{arch}" for minor in range(int(match[1]), 16, -1)]
    tags.append(f"manylinux2014_{arch}")
    if arch == "x86_64":
        tags += ["manylinux2010_x86_64", "manylinux1_x86_64"]
    return tags


def read_lines(path: Path) -> List[str]:
    lines = (line.split("#", 1)[0].strip() for line in path.read_text().splitlines())
    return [line for line in lines if line]


def find_targets(only: Optional[List[str]] = None) -> List[Target]:
    if not PACKAGES.is_dir():
        raise MirrorError(f"{PACKAGES} does not exist")
    targets = [Target(p) for p in sorted(PACKAGES.iterdir()) if p.is_dir()]
    if only:
        unknown = set(only) - {t.name for t in targets}
        if unknown:
            raise MirrorError(f"no such target: {', '.join(sorted(unknown))}")
        targets = [t for t in targets if t.name in only]
    return targets


# --- distribution files -------------------------------------------------------


def normalize(name: str) -> str:
    """PEP 503 project name normalisation."""
    return re.sub(r"[-_.]+", "-", name).lower()


def name_and_version(filename: str) -> tuple:
    if filename.endswith(".whl"):
        parts = filename[: -len(".whl")].split("-")
        if len(parts) not in (5, 6):
            raise MirrorError(f"{filename}: not a wheel file name")
        return parts[0], parts[1]
    for suffix in (".tar.gz", ".zip"):
        if filename.endswith(suffix):
            stem = filename[: -len(suffix)]
            if "-" not in stem:
                raise MirrorError(f"{filename}: not an sdist file name")
            name, version = stem.rsplit("-", 1)
            return name, version
    raise MirrorError(f"{filename}: not a wheel or sdist")


def read_metadata(path: Path) -> Dict[str, str]:
    """The core metadata headers of a wheel or sdist, or {} when absent."""
    raw = b""
    try:
        if path.name.endswith(".whl"):
            with zipfile.ZipFile(path) as archive:
                member = next(
                    (n for n in archive.namelist() if n.endswith(".dist-info/METADATA")), None
                )
                raw = archive.read(member) if member else b""
        elif path.name.endswith(".tar.gz"):
            with tarfile.open(path) as archive:
                member = next(
                    (m for m in archive.getmembers() if m.name.count("/") == 1 and m.name.endswith("/PKG-INFO")),
                    None,
                )
                handle = archive.extractfile(member) if member else None
                raw = handle.read() if handle else b""
        elif path.name.endswith(".zip"):
            with zipfile.ZipFile(path) as archive:
                member = next(
                    (n for n in archive.namelist() if n.count("/") == 1 and n.endswith("/PKG-INFO")), None
                )
                raw = archive.read(member) if member else b""
    except (zipfile.BadZipFile, tarfile.TarError) as error:
        raise MirrorError(f"{path.name}: unreadable archive: {error}") from error
    if not raw:
        return {}
    headers = email.parser.BytesHeaderParser().parsebytes(raw)
    return {key: str(value) for key, value in headers.items()}


# --- GitLab registry ----------------------------------------------------------


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # surface the 3xx as an HTTPError instead of following it


class Registry:
    """The PyPI endpoints of one GitLab project's package registry."""

    def __init__(self, api_url: str, project: str, username: str, token: str, ca_bundle: Optional[str]):
        project_ref = urllib.parse.quote(project, safe="") if not project.isdigit() else project
        self.base = f"{api_url.rstrip('/')}/projects/{project_ref}/packages/pypi"
        self.auth = "Basic " + base64.b64encode(f"{username}:{token}".encode()).decode()
        self.context = ssl.create_default_context(cafile=ca_bundle) if ca_bundle else None
        self._known: Dict[str, Set[str]] = {}

    @classmethod
    def from_env(cls) -> "Registry":
        env = os.environ
        api_url = env.get("GITLAB_API_URL") or env.get("CI_API_V4_URL")
        project = env.get("PYPI_PROJECT") or env.get("CI_PROJECT_ID")
        token = env.get("PYPI_TOKEN")
        username = env.get("PYPI_USERNAME", "__token__")
        if not token and env.get("CI_JOB_TOKEN"):
            token, username = env["CI_JOB_TOKEN"], "gitlab-ci-token"
        missing = [
            label
            for label, value in (
                ("GITLAB_API_URL (or CI_API_V4_URL)", api_url),
                ("PYPI_PROJECT (or CI_PROJECT_ID)", project),
                ("PYPI_TOKEN (or CI_JOB_TOKEN)", token),
            )
            if not value
        ]
        if missing:
            raise MirrorError("registry settings missing: " + ", ".join(missing))
        return cls(api_url, project, username, token, env.get("CA_BUNDLE") or None)

    def _request(self, request: urllib.request.Request):
        request.add_header("Authorization", self.auth)
        opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=self.context), _NoRedirect()
        )
        return opener.open(request, timeout=120)

    def existing(self, project_name: str) -> Set[str]:
        """File names the registry holds for one project, from its simple index."""
        key = normalize(project_name)
        if key not in self._known:
            url = f"{self.base}/simple/{key}"
            try:
                with self._request(urllib.request.Request(url)) as response:
                    page = response.read().decode("utf-8", "replace")
            except urllib.error.HTTPError as error:
                # 404: nothing by that name. 3xx: GitLab's "forward PyPI requests"
                # setting sends a name it does not hold to pypi.org. Following
                # that redirect would read PyPI's file list as ours and skip
                # every upload.
                if error.code != 404 and not 300 <= error.code < 400:
                    raise MirrorError(f"GET {url}: HTTP {error.code}") from error
                page = ""
            self._known[key] = {html.unescape(m) for m in re.findall(r">([^<>]+)</a>", page)}
        return self._known[key]

    def upload(self, path: Path) -> None:
        name, version = name_and_version(path.name)
        data = path.read_bytes()
        fields = {
            "name": name,
            "version": version,
            "sha256_digest": hashlib.sha256(data).hexdigest(),
            "md5_digest": hashlib.md5(data).hexdigest(),  # noqa: S324 - registry field, not security
        }
        requires_python = read_metadata(path).get("Requires-Python")
        if requires_python:
            fields["requires_python"] = requires_python
        boundary = uuid.uuid4().hex
        body = bytearray()
        for key, value in fields.items():
            body += (
                f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'
            ).encode()
        body += (
            f'--{boundary}\r\nContent-Disposition: form-data; name="content"; filename="{path.name}"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n"
        ).encode()
        body += data + f"\r\n--{boundary}--\r\n".encode()
        request = urllib.request.Request(self.base, data=bytes(body), method="POST")
        request.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
        try:
            with self._request(request):
                pass
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", "replace")[:300]
            if error.code in (400, 409) and "taken" in detail.lower():
                return  # uploaded by a concurrent run between our check and our upload
            raise MirrorError(f"upload {path.name}: HTTP {error.code}: {detail}") from error
        self._known.setdefault(normalize(name), set()).add(path.name)


# --- commands -----------------------------------------------------------------


def cmd_targets(args) -> int:
    for target in find_targets(args.target):
        requirements = target.path / "requirements.txt"
        print(
            f"{target.name:28} python {target.python:5} {target.os:8} {target.arch:8} "
            f"platforms={target.platforms[0]}{f' (+{len(target.platforms) - 1} older)' if len(target.platforms) > 1 else ''} "
            f"requirements={len(read_lines(requirements)) if requirements.is_file() else 0} "
            f"downloaded={len(target.files())}"
        )
    return 0


def pip_download(target: Target, extra: List[str]) -> None:
    target.wheelhouse.mkdir(exist_ok=True)
    base = [sys.executable, "-m", "pip", "download", "--dest", str(target.wheelhouse),
            "--disable-pip-version-check", "--progress-bar", "off", *extra]
    requirements = target.path / "requirements.txt"
    if requirements.is_file() and read_lines(requirements):
        command = base + ["-r", str(requirements), "--only-binary=:all:",
                          "--python-version", target.python, "--implementation", "cp"]
        for platform in target.platforms:
            command += ["--platform", platform]
        print(f"==> {target.name}: {' '.join(command[3:])}", flush=True)
        subprocess.run(command, check=True)
    # Packages published only as source. pip will not resolve a foreign
    # platform's dependencies for an sdist, so these come without dependencies:
    # list those in requirements.txt (as wheels) or here.
    sdists = target.path / "sdist.txt"
    if sdists.is_file() and read_lines(sdists):
        command = base + ["-r", str(sdists), "--no-deps", "--no-binary=:all:"]
        print(f"==> {target.name} (sdist): {' '.join(command[3:])}", flush=True)
        subprocess.run(command, check=True)


def cmd_download(args) -> int:
    failed = []
    for target in find_targets(args.target):
        try:
            pip_download(target, args.pip_arg or [])
        except subprocess.CalledProcessError:
            failed.append(target.name)
            print(f"ERROR: pip download failed for {target.name}", file=sys.stderr)
    if failed:
        print(f"ERROR: {len(failed)} target(s) failed: {', '.join(failed)}", file=sys.stderr)
        return 1
    return 0


def distinct_files(targets: Iterable[Target]) -> Dict[str, Path]:
    """One path per file name: the same wheel downloaded for two targets is one upload."""
    files: Dict[str, Path] = {}
    for target in targets:
        for path in target.files():
            files.setdefault(path.name, path)
    return files


def cmd_publish(args) -> int:
    registry = Registry.from_env()
    files = distinct_files(find_targets(args.target))
    uploaded = skipped = 0
    for filename, path in sorted(files.items()):
        name, _ = name_and_version(filename)
        if filename in registry.existing(name):
            skipped += 1
            continue
        if args.dry_run:
            print(f"would upload {filename}")
        else:
            registry.upload(path)
            print(f"uploaded {filename}", flush=True)
        uploaded += 1
    verb = "to upload" if args.dry_run else "uploaded"
    print(f"{len(files)} file(s): {uploaded} {verb}, {skipped} already in the registry")
    return 0


def cmd_prune(args) -> int:
    registry = Registry.from_env()
    removed = 0
    for target in find_targets(args.target):
        for path in target.files():
            name, _ = name_and_version(path.name)
            if path.name in registry.existing(name):
                path.unlink()
                removed += 1
    print(f"removed {removed} file(s) the registry already holds")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    for name, handler, text in (
        ("targets", cmd_targets, "list the targets under packages/ and how each is read"),
        ("download", cmd_download, "pip download every target into its wheelhouse/"),
        ("publish", cmd_publish, "upload wheelhouse files the registry does not have"),
        ("prune", cmd_prune, "delete wheelhouse files the registry already has"),
    ):
        command = sub.add_parser(name, help=text)
        command.add_argument("--target", action="append", help="limit to this target (repeatable)")
        command.set_defaults(handler=handler)
        if name == "download":
            command.add_argument("--pip-arg", action="append", help="extra argument for pip download (repeatable)")
        if name == "publish":
            command.add_argument("--dry-run", action="store_true", help="list what would be uploaded")
    args = parser.parse_args(argv)
    try:
        return args.handler(args)
    except MirrorError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
