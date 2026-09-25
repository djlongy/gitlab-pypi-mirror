#!/usr/bin/env python3
"""Mirror Python packages into a GitLab PyPI package registry.

Two halves, one file, standard library only:

  On a machine that can reach PyPI (directly or through an HTTP proxy):
      mirror.py download      pip-download every packages/<target>/requirements.txt
      mirror.py prune         remove local copies of wheels the registry already has

  In GitLab CI, where the runner cannot reach PyPI:
      mirror.py publish       upload every downloaded file the registry lacks
                              (--bundle DIR: also tar just those files for the high side)

  Moving packages across an air gap:
      mirror.py export        tar every file the registry received since a date
      mirror.py import        verify a tar and upload what the registry here lacks

A target is a directory under packages/ named <os>-py<X.Y>[-<arch>]:

  packages/linux-py3.12/requirements.txt
  packages/windows-py3.9/requirements.txt
  packages/linux-py3.11-aarch64/requirements.txt

Every target downloads into one wheelhouse/ at the top of the repository. A
wheel's file name carries its Python and platform tags, so targets never
collide and a pure-Python wheel is stored once. Run `mirror.py targets` to see how
each directory name is read.

Python 3.8 or newer.
"""

from __future__ import annotations

import argparse
import base64
import email.parser
import hashlib
import html
import io
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

ROOT = Path(__file__).resolve().parent
PACKAGES = ROOT / "packages"
WHEELHOUSE = "wheelhouse"

TARGET_RE = re.compile(r"^(?P<os>linux|windows)-py(?P<py>3\.\d{1,2})(?:-(?P<arch>[a-z0-9_]+))?$")
DEFAULT_ARCH = {"linux": "x86_64", "windows": "amd64"}
ARCHES = {"linux": {"x86_64", "aarch64"}, "windows": {"amd64", "arm64", "win32"}}
# Newest glibc the Linux hosts have: 2_28 is RHEL 8 and later. pip does not
# expand a PEP 600 tag to the older ones, so linux_platforms() lists them all.
MANYLINUX = os.environ.get("MANYLINUX", "2_28")


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
    def platforms(self) -> List[str]:
        override = self.path / "platforms.txt"
        if override.is_file():
            return read_lines(override)
        if self.os == "windows":
            return ["win32"] if self.arch == "win32" else [f"win_{self.arch}"]
        return linux_platforms(MANYLINUX, self.arch)

    def definition(self) -> List[Path]:
        """The files that say what this target needs: requirements.txt and platforms.txt."""
        return [p for p in (self.path / "requirements.txt", self.path / "platforms.txt") if p.is_file()]


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


def wheelhouse() -> Path:
    return ROOT / WHEELHOUSE


def adopt_target_wheelhouses() -> None:
    """Move packages/<target>/wheelhouse/ files into the shared wheelhouse, one copy per name."""
    for old in sorted(PACKAGES.glob(f"*/{WHEELHOUSE}")):
        wheelhouse().mkdir(exist_ok=True)
        for path in old.iterdir():
            if (wheelhouse() / path.name).exists():
                path.unlink()  # a file name on PyPI never changes content
            else:
                path.replace(wheelhouse() / path.name)
        old.rmdir()
        print(f"moved {old.relative_to(ROOT)}/ into {WHEELHOUSE}/")


def wheel_files() -> List[Path]:
    """The wheels in the wheelhouse. Nothing else is ever published."""
    adopt_target_wheelhouses()
    if not wheelhouse().is_dir():
        return []
    files = sorted(p for p in wheelhouse().iterdir() if p.is_file())
    for path in files:
        if not path.name.endswith(".whl"):
            print(f"WARNING: ignoring {path.relative_to(ROOT)}: only wheels are published", file=sys.stderr)
    return [p for p in files if p.name.endswith(".whl")]


# --- distribution files -------------------------------------------------------


def normalize(name: str) -> str:
    """PEP 503 project name normalisation."""
    return re.sub(r"[-_.]+", "-", name).lower()


def name_and_version(filename: str) -> tuple:
    parts = filename[: -len(".whl")].split("-") if filename.endswith(".whl") else []
    if len(parts) not in (5, 6):
        raise MirrorError(f"{filename}: not a wheel file name")
    return parts[0], parts[1]


def read_metadata(path: Path) -> Dict[str, str]:
    """The core metadata headers of a wheel, or {} when it has none."""
    try:
        with zipfile.ZipFile(path) as archive:
            member = next((n for n in archive.namelist() if n.endswith(".dist-info/METADATA")), None)
            raw = archive.read(member) if member else b""
    except zipfile.BadZipFile as error:
        with open(path, "rb") as handle:
            if handle.read(40).startswith(b"version https://git-lfs"):
                raise MirrorError(f"{path.name}: a Git LFS pointer, not a wheel. This checkout has no git-lfs: "
                                  "install it where this runs, or delete .gitattributes and commit the wheels as "
                                  "ordinary files") from error
        raise MirrorError(f"{path.name}: unreadable wheel: {error}") from error
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
        self.project_api = f"{api_url.rstrip('/')}/projects/{project_ref}"
        self.base = f"{self.project_api}/packages/pypi"
        self.auth = "Basic " + base64.b64encode(f"{username}:{token}".encode()).decode()
        self.token, self.job_token = token, username == "gitlab-ci-token"
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
        # The package endpoints take Basic auth. The REST API ignores it and answers 404.
        if request.full_url.startswith(self.base):
            request.add_header("Authorization", self.auth)
        else:
            request.add_header("PRIVATE-TOKEN", self.token)
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

    def _get(self, url: str) -> bytes:
        try:
            with self._request(urllib.request.Request(url)) as response:
                return response.read()
        except urllib.error.HTTPError as error:
            raise MirrorError(f"GET {url}: HTTP {error.code}") from error

    def files_since(self, since: str) -> List[Dict[str, str]]:
        """Every PyPI file the registry received on or after `since` (YYYY-MM-DD), from the packages API.

        A version gains files over time (one wheel per platform), so the date is read per file.
        A CI job token cannot list packages, so this needs PYPI_TOKEN with read_api."""
        if self.job_token:
            raise MirrorError("export lists packages through the packages API, which a CI job token cannot read: "
                              "set PYPI_TOKEN (EXPORT_TOKEN in CI) to a project access token with read_api (Reporter role)")
        found, page = [], 1
        while True:
            url = f"{self.project_api}/packages?package_type=pypi&per_page=100&page={page}"
            packages = json.loads(self._get(url))
            for package in packages:
                files_url = f"{self.project_api}/packages/{package['id']}/package_files?per_page=100"
                for item in json.loads(self._get(files_url)):
                    if item["created_at"][:10] >= since and item["file_name"].endswith(".whl"):
                        found.append({"file": item["file_name"], "sha256": item["file_sha256"]})
            if len(packages) < 100:
                return sorted(found, key=lambda f: f["file"])
            page += 1

    def download(self, filename: str, sha256: str, dest: Path) -> Path:
        path = dest / filename
        data = self._get(f"{self.base}/files/{sha256}/{urllib.parse.quote(filename)}")
        if hashlib.sha256(data).hexdigest() != sha256:
            raise MirrorError(f"{filename}: sha256 mismatch after download")
        path.write_bytes(data)
        return path

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


# --- bundles ------------------------------------------------------------------
#
# A bundle is one tar: wheels/<file> for each file plus MANIFEST.json listing every
# file with its sha256, and a <bundle>.sha256 beside it. It carries only the files
# named, so a scheduled run ships what changed instead of the whole collection.


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_bundle(files: List[Path], out_dir: Path, kind: str) -> Optional[Path]:
    if not files:
        print("bundle: nothing new, no bundle written")
        return None
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    path = out_dir / f"pypi-{kind}-{stamp}.tar"
    definitions = [(f"{t.name}/{p.name}", p) for t in find_targets() for p in t.definition()]
    manifest = {"created": stamp, "kind": kind, "source": os.environ.get("CI_PROJECT_PATH", ""),
                "commit": os.environ.get("CI_COMMIT_SHA", ""),
                "files": [{"file": f.name, "sha256": sha256_file(f), "size": f.stat().st_size}
                          for f in sorted(files, key=lambda f: f.name)],
                "requirements": [{"file": name, "sha256": sha256_file(p)} for name, p in definitions]}
    tmp = path.with_name("." + path.name + ".tmp")
    with tarfile.open(tmp, "w", format=tarfile.PAX_FORMAT) as tar:
        data = (json.dumps(manifest, indent=1) + "\n").encode()
        info = tarfile.TarInfo("MANIFEST.json")
        info.size, info.mtime = len(data), int(time.time())
        tar.addfile(info, fileobj=io.BytesIO(data))
        for f in sorted(files, key=lambda f: f.name):
            tar.add(f, arcname=f"wheels/{f.name}", recursive=False)
        for name, p in definitions:
            tar.add(p, arcname=f"requirements/{name}", recursive=False)
    os.replace(tmp, path)
    path.with_name(path.name + ".sha256").write_text(f"{sha256_file(path)}  {path.name}\n")
    total = sum(entry["size"] for entry in manifest["files"])
    print(f"bundle: {path} ({len(files)} file(s), {total / 1e6:.1f} MB)")
    return path


def read_bundle(path: Path, into: Path) -> List[Tuple[Path, str]]:
    """Verify a bundle and its checksum, unpack it, and return (wheel, sha256) pairs."""
    sidecar = path.with_name(path.name + ".sha256")
    if sidecar.is_file() and sidecar.read_text().split()[0] != sha256_file(path):
        raise MirrorError(f"{path.name}: checksum does not match {sidecar.name}")
    with tarfile.open(path) as tar:
        for member in tar.getmembers():
            if not (member.isfile() or member.isdir()) or member.name.startswith("/") or ".." in Path(member.name).parts:
                raise MirrorError(f"{path.name}: refusing unsafe entry {member.name!r}")
        tar.extractall(into)
    manifest = json.loads((into / "MANIFEST.json").read_text())
    out = []
    for entry in manifest["files"]:
        wheel = into / "wheels" / entry["file"]
        if not wheel.is_file() or sha256_file(wheel) != entry["sha256"]:
            raise MirrorError(f"{path.name}: {entry['file']} is missing or does not match its sha256")
        out.append((wheel, entry["sha256"]))
    for entry in manifest.get("requirements", []):
        if sha256_file(into / "requirements" / entry["file"]) != entry["sha256"]:
            raise MirrorError(f"{path.name}: requirements/{entry['file']} does not match its sha256")
    return out


# --- commands -----------------------------------------------------------------


def cmd_targets(args) -> int:
    for target in find_targets(args.target):
        requirements = target.path / "requirements.txt"
        print(
            f"{target.name:28} python {target.python:5} {target.os:8} {target.arch:8} "
            f"platforms={target.platforms[0]}{f' (+{len(target.platforms) - 1} older)' if len(target.platforms) > 1 else ''} "
            f"requirements={len(read_lines(requirements)) if requirements.is_file() else 0}"
        )
    return 0


def pip_download(target: Target, extra: List[str]) -> None:
    wheelhouse().mkdir(exist_ok=True)
    base = [sys.executable, "-m", "pip", "download", "--dest", str(wheelhouse()),
            "--disable-pip-version-check", "--progress-bar", "off", *extra]
    requirements = target.path / "requirements.txt"
    if requirements.is_file() and read_lines(requirements):
        # Wheels only, for every package and every dependency. A source
        # distribution needs a compiler and build dependencies on the
        # air-gapped host; a package with no wheel for this target fails the
        # download here instead of failing the install there.
        command = base + ["-r", str(requirements), "--only-binary=:all:",
                          "--python-version", target.python, "--implementation", "cp"]
        for platform in target.platforms:
            command += ["--platform", platform]
        print(f"==> {target.name}: {' '.join(command[3:])}", flush=True)
        subprocess.run(command, check=True)


def cmd_download(args) -> int:
    failed = []
    adopt_target_wheelhouses()
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


def cmd_publish(args) -> int:
    registry = Registry.from_env()
    files = wheel_files()
    uploaded, skipped = [], 0
    for path in files:
        filename = path.name
        name, _ = name_and_version(filename)
        if filename in registry.existing(name):
            skipped += 1
            continue
        if args.dry_run:
            print(f"would upload {filename}")
        else:
            registry.upload(path)
            print(f"uploaded {filename}", flush=True)
        uploaded.append(path)
    verb = "to upload" if args.dry_run else "uploaded"
    print(f"{len(files)} file(s): {len(uploaded)} {verb}, {skipped} already in the registry")
    if args.bundle and not args.dry_run:
        write_bundle(uploaded, Path(args.bundle), "delta")
    return 0


def cmd_export(args) -> int:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", args.since):
        raise MirrorError(f"--since {args.since!r}: expected YYYY-MM-DD")
    registry = Registry.from_env()
    wanted = registry.files_since(args.since)
    print(f"{len(wanted)} file(s) received since {args.since}")
    with tempfile.TemporaryDirectory() as tmp:
        paths = [registry.download(f["file"], f["sha256"], Path(tmp)) for f in wanted]
        write_bundle(paths, Path(args.bundle), f"since-{args.since}")
    return 0


def cmd_import(args) -> int:
    registry = Registry.from_env()
    uploaded = skipped = 0
    with tempfile.TemporaryDirectory() as tmp:
        for bundle in args.bundle:
            unpacked = Path(tmp) / Path(bundle).stem
            wheels = read_bundle(Path(bundle), unpacked)
            if args.requirements and (unpacked / "requirements").is_dir():
                # Later bundles overwrite earlier ones, so pass them oldest first.
                shutil.copytree(unpacked / "requirements", args.requirements, dirs_exist_ok=True)
                print(f"requirements from {Path(bundle).name} written to {args.requirements}")
            for wheel, _ in wheels:
                name, _ = name_and_version(wheel.name)
                if wheel.name in registry.existing(name):
                    skipped += 1
                    continue
                if args.dry_run:
                    print(f"would upload {wheel.name}")
                else:
                    registry.upload(wheel)
                    print(f"uploaded {wheel.name}", flush=True)
                uploaded += 1
    verb = "to upload" if args.dry_run else "uploaded"
    print(f"{uploaded + skipped} file(s): {uploaded} {verb}, {skipped} already in the registry")
    return 0


def cmd_prune(args) -> int:
    registry = Registry.from_env()
    removed = 0
    for path in wheel_files():
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
        ("download", cmd_download, "pip download every target into wheelhouse/"),
        ("publish", cmd_publish, "upload wheelhouse files the registry does not have"),
        ("prune", cmd_prune, "remove local copies of wheels the registry already has"),
        ("export", cmd_export, "tar every file the registry received since a date"),
        ("import", cmd_import, "verify bundles and upload what the registry lacks"),
    ):
        command = sub.add_parser(name, help=text)
        if name in ("targets", "download"):
            command.add_argument("--target", action="append", help="limit to this target (repeatable)")
        command.set_defaults(handler=handler)
        if name == "download":
            command.add_argument("--pip-arg", action="append", help="extra argument for pip download (repeatable)")
        if name in ("publish", "import"):
            command.add_argument("--dry-run", action="store_true", help="list what would be uploaded")
        if name == "publish":
            command.add_argument("--bundle", metavar="DIR", help="also write the files uploaded by this run to a tar in DIR")
        if name == "export":
            command.add_argument("--since", required=True, help="YYYY-MM-DD, first day to include")
            command.add_argument("--bundle", metavar="DIR", default="bundle", help="directory for the tar (default: bundle)")
        if name == "import":
            command.add_argument("bundle", nargs="+", help="pypi-*.tar written by publish --bundle or export, oldest first")
            command.add_argument("--requirements", metavar="DIR",
                                 help="also write the bundled requirements.txt and platforms.txt to DIR/<target>/")
    args = parser.parse_args(argv)
    try:
        return args.handler(args)
    except MirrorError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
