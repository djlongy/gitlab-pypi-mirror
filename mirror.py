#!/usr/bin/env python3
"""Mirror Python packages into a GitLab PyPI package registry.

  %(prog)s sync      pip download every packages/<target>/requirements.txt into wheelhouse/,
                      build a wheel where PyPI has only an sdist, upload what the registry lacks,
                      and with MIRROR_REQUEST_URL ask the low NiFi to fetch each new wheel
  %(prog)s export    tar wheels from the registry, by package or by date, to carry by hand
  %(prog)s import    verify a tar and upload what the registry here lacks

A target is a directory under packages/ named <os>-py<X.Y>[-<arch>]:

  packages/linux-py3.12/requirements.txt
  packages/windows-py3.9/requirements.txt
  packages/linux-py3.11-aarch64/requirements.txt

Python 3.8 or newer, standard library only.
"""

from __future__ import annotations

import argparse
import base64
import email.parser
import hashlib
import html
import http.client
import io
import json
import os
import queue as queue_module
import re
import shutil
import ssl
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

VERSION = "0.2.0"
ROOT = Path(__file__).resolve().parent
PACKAGES = ROOT / "packages"
WHEELHOUSE = "wheelhouse"

TARGET_RE = re.compile(r"^(?P<os>linux|windows)-py(?P<py>3\.\d{1,2})(?:-(?P<arch>[a-z0-9_]+))?$")
DEFAULT_ARCH = {"linux": "x86_64", "windows": "amd64"}
ARCHES = {"linux": {"x86_64", "aarch64"}, "windows": {"amd64", "arm64", "win32"}}
# X-Artifact-Action on every request POST, for RouteOnAttribute on a NiFi shared with other feeds.
ARTIFACT_ACTION = "mirror"
HEARTBEAT = 15.0  # seconds of pip silence before a progress line
STAGING: Optional[Path] = None  # sync sets it: TMPDIR for pip and source builds

class MirrorError(Exception):
    pass


class UsageError(MirrorError):
    """A setting that is wrong before any work starts: exit 2, like a bad option."""


TRUE_WORDS = ("1", "yes", "y", "true", "t", "on")
FALSE_WORDS = ("0", "no", "n", "false", "f", "off")


def env_flag(name: str, default: bool = False) -> bool:
    """A boolean environment variable: 1 yes y true t on, or 0 no n false f off, any case.

    Unset or empty is the default; anything else is a usage error."""
    value = os.environ.get(name, "").strip().lower()
    if not value:
        return default
    if value in TRUE_WORDS:
        return True
    if value in FALSE_WORDS:
        return False
    raise UsageError(f"{name}={os.environ[name]!r} is not a boolean; use 1, yes, y, true, t, on or 0, no, n, false, f, off")


QUIET = False


def prog() -> str:
    """The name this program was started as, so messages match how it is run."""
    return Path(sys.argv[0]).name or "pypi-mirror"


def progress(message: str) -> None:
    """What the program is doing, on stderr so stdout carries data only. -q silences it."""
    if not QUIET:
        print(message, file=sys.stderr, flush=True)


def warn(message: str) -> None:
    print(f"{prog()}: warning: {message}", file=sys.stderr, flush=True)


def fail(message) -> None:
    print(f"{prog()}: {message}", file=sys.stderr, flush=True)


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
        return linux_platforms(manylinux(), self.arch)


def manylinux() -> str:
    return os.environ.get("MIRROR_MANYLINUX", "2_28")


def linux_platforms(glibc: str, arch: str) -> List[str]:
    """Every manylinux tag a host with this glibc can install, newest first."""
    match = re.fullmatch(r"2_(\d+)", glibc)
    if not match or int(match[1]) < 17:
        raise UsageError(f"MIRROR_MANYLINUX={glibc!r}: expected 2_<minor>, minor 17 or newer")
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
    """Where downloads and builds land: MIRROR_WHEELHOUSE_DIR (a large disk, on a shell runner whose builds
    directory is small), else wheelhouse/ in the checkout."""
    return Path(os.environ.get("MIRROR_WHEELHOUSE_DIR") or ROOT / WHEELHOUSE)


def shown(path: Path) -> str:
    """path relative to the checkout when it is inside it."""
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def wheel_files() -> List[Path]:
    """The wheels in the wheelhouse, and its source distributions with MIRROR_PUBLISH_SDISTS. Nothing else is
    ever published."""
    # Wheels committed to wheelhouse/ are published too when MIRROR_WHEELHOUSE_DIR is elsewhere:
    # pushing a wheel there is the fallback when nothing else can fetch it.
    seen: Dict[str, Path] = {}
    for folder in dict.fromkeys([wheelhouse(), ROOT / WHEELHOUSE]):
        for path in (sorted(folder.iterdir()) if folder.is_dir() else []):
            if path.is_file():
                seen.setdefault(path.name, path)
    files = sorted(seen.values())
    for path in files:
        if not published(path.name):
            warn(f"ignoring {shown(path)}: only wheels are published"
                 + ("" if path.name.endswith(".whl") or not is_sdist(path.name) else " (MIRROR_PUBLISH_SDISTS=true adds sdists)"))
    return [p for p in files if published(p.name)]


# --- distribution files -------------------------------------------------------


def normalize(name: str) -> str:
    """PEP 503 project name normalisation."""
    return re.sub(r"[-_.]+", "-", name).lower()


SDIST_SUFFIXES = (".tar.gz", ".zip")
SDIST_RE = re.compile(r"([A-Za-z0-9][A-Za-z0-9._+!-]*)-([0-9][A-Za-z0-9._+!]*)")


def is_sdist(filename: str) -> bool:
    """A source distribution file name: name-version.tar.gz or .zip (PEP 625, and the older hyphenated names)."""
    stem = next((filename[: -len(s)] for s in SDIST_SUFFIXES if filename.endswith(s)), "")
    return bool(SDIST_RE.fullmatch(stem))


def published(filename: str) -> bool:
    """What publish, request and export carry: wheels, and sdists only with MIRROR_PUBLISH_SDISTS=true. Wheels
    install on the high side without a compiler; an sdist makes pip build it there."""
    return filename.endswith(".whl") or (is_sdist(filename) and env_flag("MIRROR_PUBLISH_SDISTS"))


def name_and_version(filename: str) -> tuple:
    if is_sdist(filename):
        stem = next(filename[: -len(s)] for s in SDIST_SUFFIXES if filename.endswith(s))
        return tuple(SDIST_RE.fullmatch(stem).groups())
    parts = filename[: -len(".whl")].split("-") if filename.endswith(".whl") else []
    if len(parts) not in (5, 6):
        raise MirrorError(f"{filename}: not a wheel file name")
    return parts[0], parts[1]


METADATA_LIMIT = 4 << 20  # torch's METADATA is 40 KB; a few MB is any real one, a zip bomb is not
SDIST_SCAN_LIMIT = 1 << 30  # bytes of a tar.gz decompressed while looking for PKG-INFO, so a gzip bomb stops


def read_metadata(path: Path) -> Dict[str, str]:
    """The core metadata headers of a wheel (its top-level .dist-info/METADATA, which pip reads) or an sdist (its
    top-level PKG-INFO), or {} when it has none. Each value on one line: a folded header cannot become a newline."""
    if path.name.endswith(".whl"):
        noun, kind = "wheel", "METADATA"
        member_of = lambda name: name.count("/") == 1 and name.endswith(".dist-info/METADATA")  # noqa: E731
    elif is_sdist(path.name):
        noun, kind = "source distribution", "PKG-INFO"
        member_of = lambda name: name.count("/") == 1 and name.endswith("/PKG-INFO")  # noqa: E731
    else:
        return {}
    try:
        if path.name.endswith(".tar.gz"):
            # ponytail: a tar.gz has no index, so this reads up to PKG-INFO; it is near the start in every sdist seen.
            with tarfile.open(path, "r:gz") as archive:
                member = None
                for entry in archive:
                    if entry.isfile() and member_of(entry.name):
                        member = entry
                        break
                    if archive.offset > SDIST_SCAN_LIMIT:
                        warn(f"{path.name}: no PKG-INFO in its first {SDIST_SCAN_LIMIT} bytes, read without metadata")
                        break
                raw = archive.extractfile(member).read(METADATA_LIMIT + 1) if member else b""
        else:
            with zipfile.ZipFile(path) as archive:
                members = [i for i in archive.infolist() if member_of(i.filename)]
                if len(members) > 1:  # pip refuses such a wheel too
                    raise MirrorError(f"{path.name}: {len(members)} top-level {kind} files, a {noun} has one")
                member = members[0] if members else None
                if member and member.file_size > METADATA_LIMIT:
                    raise MirrorError(f"{path.name}: {kind} is {member.file_size} bytes, more than {METADATA_LIMIT}")
                # Read no more than the limit whatever the header says the size is.
                raw = archive.open(member).read(METADATA_LIMIT + 1) if member else b""
    except (zipfile.BadZipFile, tarfile.TarError, EOFError, OSError) as error:
        with open(path, "rb") as handle:
            if handle.read(40).startswith(b"version https://git-lfs"):
                raise MirrorError(f"{path.name}: a Git LFS pointer, not a {noun}. This checkout has no git-lfs: "
                                  "install it where this runs, or delete .gitattributes and commit the wheels as "
                                  "ordinary files") from error
        raise MirrorError(f"{path.name}: unreadable {noun}: {error}") from error
    if len(raw) > METADATA_LIMIT:
        raise MirrorError(f"{path.name}: {kind} is more than {METADATA_LIMIT} bytes")
    if not raw:
        return {}
    headers = email.parser.BytesHeaderParser().parsebytes(raw)
    return {key: " ".join(str(value).split()) for key, value in headers.items()}


# --- GitLab registry ----------------------------------------------------------


CHUNK = 1 << 20
UPLOAD_TIMEOUT = 3600  # seconds without progress; GitLab answers a multi-GB upload only after it has stored the file


class MultipartFile:
    """A multipart/form-data body read in chunks: the form fields, then the file, then the closing boundary."""

    def __init__(self, fields: Dict[str, str], path: Path):
        self.boundary = uuid.uuid4().hex
        head = bytearray()
        for key, value in fields.items():
            head += f'--{self.boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode()
        head += (f'--{self.boundary}\r\nContent-Disposition: form-data; name="content"; filename="{path.name}"\r\n'
                 "Content-Type: application/octet-stream\r\n\r\n").encode()
        self._head, self._tail = bytes(head), f"\r\n--{self.boundary}--\r\n".encode()
        self._file = open(path, "rb")
        self.length = len(self._head) + path.stat().st_size + len(self._tail)

    def read(self, size: int = CHUNK) -> bytes:
        if self._head:
            data, self._head = self._head[:size], self._head[size:]
            return data
        data = self._file.read(size)
        if data:
            return data
        data, self._tail = self._tail[:size], self._tail[size:]
        return data

    def close(self) -> None:
        self._file.close()


def size_hint(code: int, detail: str, size: int) -> str:
    """Name the size limit when a refusal looks like one: a 413, a reset connection, or a message that says so."""
    if code in (0, 413) or re.search(r"too (large|big)|file size|max(imum)? .*size|exceed", detail, re.I):
        return (f". The file is {size / 1e9:.2f} GB: GitLab refuses a PyPI file over its maximum PyPI package file size "
                "(default 5 GB, the max_pypi_package_file_size application setting), and a reverse proxy in front of "
                "it may refuse a smaller body (nginx client_max_body_size). Raise the limit, or split the package")
    return ""


def refuse_cleartext(url: str, what: str) -> None:
    """A credential goes over https, or plain http only to this machine (tests, a local proxy)."""
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" and parsed.hostname not in ("127.0.0.1", "::1", "localhost"):
        raise MirrorError(f"{url}: refusing to send {what} over {parsed.scheme or 'no scheme'}; use https")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # surface the 3xx as an HTTPError instead of following it


class Registry:
    """The PyPI endpoints of one GitLab project's package registry."""

    def __init__(self, api_url: str, project: str, username: str, token: str, ca_bundle: Optional[str]):
        refuse_cleartext(api_url, "the registry token")
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
        api_url = env.get("MIRROR_GITLAB_API_URL") or env.get("CI_API_V4_URL")
        project = env.get("MIRROR_PROJECT") or env.get("CI_PROJECT_ID")
        token = env.get("MIRROR_TOKEN")
        username = env.get("MIRROR_USERNAME", "__token__")
        if not token and env.get("CI_JOB_TOKEN"):
            token, username = env["CI_JOB_TOKEN"], "gitlab-ci-token"
        missing = [
            label
            for label, value in (
                ("--api-url or MIRROR_GITLAB_API_URL (or CI_API_V4_URL)", api_url),
                ("--project or MIRROR_PROJECT (or CI_PROJECT_ID)", project),
                ("MIRROR_TOKEN (or CI_JOB_TOKEN)", token),
            )
            if not value
        ]
        if missing:
            raise MirrorError("registry settings missing: " + ", ".join(missing))
        return cls(api_url, project, username, token, env.get("MIRROR_CA_BUNDLE") or None)

    def _request(self, request: urllib.request.Request, timeout: int = 120):
        # The package endpoints take Basic auth. The REST API ignores it and answers 404.
        if request.full_url.startswith(self.base):
            request.add_header("Authorization", self.auth)
        else:
            request.add_header("PRIVATE-TOKEN", self.token)
        opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=self.context), _NoRedirect()
        )
        return opener.open(request, timeout=timeout)

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
        A CI job token cannot list packages, so this needs MIRROR_TOKEN with read_api."""
        if self.job_token:
            where = ("add a masked CI/CD variable EXPORT_TOKEN" if os.environ.get("GITLAB_CI")
                     else "set MIRROR_TOKEN")
            raise MirrorError("export lists packages through the packages API, which a CI job token cannot read: "
                              f"{where} holding a project access token with the Reporter role and read_api scope")
        found, page = [], 1
        while True:
            url = f"{self.project_api}/packages?package_type=pypi&per_page=100&page={page}"
            packages = json.loads(self._get(url))
            for package in packages:
                for item in self._files_of(package["id"]):
                    if item["created_at"][:10] >= since and published(item["file_name"]):
                        found.append({"file": item["file_name"], "sha256": item["file_sha256"]})
            if len(packages) < 100:
                return sorted(found, key=lambda f: f["file"])
            page += 1

    def _files_of(self, package_id) -> List[dict]:
        """Every file of one package, across pages."""
        found, page = [], 1
        while True:
            batch = json.loads(self._get(f"{self.project_api}/packages/{package_id}/package_files?per_page=100&page={page}"))
            found += batch
            if len(batch) < 100:
                return found
            page += 1

    def package_files(self, name: str, version: Optional[str] = None) -> List[Dict[str, str]]:
        """Every wheel the registry holds for one package, or one version of it, from the packages API."""
        if self.job_token:
            raise MirrorError("export lists packages through the packages API, which a CI job token cannot read: "
                              "set MIRROR_TOKEN or EXPORT_TOKEN to a token with read_api")
        found, page, key = [], 1, normalize(name)
        # package_name is a substring match on the name as uploaded, whose separators may differ
        # (my_pkg, My.Pkg): ask for its longest separator-free part, then compare normalized names.
        probe = max(re.split(r"[-_.]+", key), key=len)
        while True:
            url = (f"{self.project_api}/packages?package_type=pypi&per_page=100&page={page}"
                   f"&package_name={urllib.parse.quote(probe)}")
            packages = json.loads(self._get(url))
            for package in packages:
                if normalize(package["name"]) != key or (
                        version and canonical_version(package["version"]) != canonical_version(version)):
                    continue
                found += [{"file": item["file_name"], "sha256": item["file_sha256"]}
                          for item in self._files_of(package["id"]) if published(item["file_name"])]
            if len(packages) < 100:
                return sorted(found, key=lambda f: f["file"])
            page += 1

    def download(self, filename: str, sha256: str, dest: Path) -> Path:
        """Stream one file to disk, so a multi-GB wheel never sits in memory."""
        if Path(filename).name != filename or filename in (".", ".."):
            raise MirrorError(f"refusing a registry file name with a path in it: {filename!r}")
        path, digest = dest / filename, hashlib.sha256()
        url = f"{self.base}/files/{sha256}/{urllib.parse.quote(filename)}"
        try:
            with self._request(urllib.request.Request(url)) as response, open(path, "wb") as out:
                for chunk in iter(lambda: response.read(1 << 20), b""):
                    digest.update(chunk)
                    out.write(chunk)
        except urllib.error.HTTPError as error:
            raise MirrorError(f"GET {url}: HTTP {error.code}") from error
        if digest.hexdigest() != sha256:
            path.unlink()
            raise MirrorError(f"{filename}: sha256 mismatch after download")
        return path

    def upload(self, path: Path) -> None:
        """POST one wheel as multipart/form-data, streamed from disk: a multi-GB wheel is never held in memory."""
        name, version = name_and_version(path.name)
        sha256, md5 = hashlib.sha256(), hashlib.md5()  # noqa: S324 - registry field, not security
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(CHUNK), b""):
                sha256.update(chunk)
                md5.update(chunk)
        fields = {
            "name": name,
            "version": version,
            "sha256_digest": sha256.hexdigest(),
            "md5_digest": md5.hexdigest(),
        }
        if is_sdist(path.name):
            fields.update(filetype="sdist", pyversion="source")
        requires_python = read_metadata(path).get("Requires-Python")
        if requires_python:
            fields["requires_python"] = requires_python
        body = MultipartFile(fields, path)
        request = urllib.request.Request(self.base, data=body, method="POST")
        request.add_header("Content-Type", f"multipart/form-data; boundary={body.boundary}")
        request.add_header("Content-Length", str(body.length))
        try:
            with self._request(request, timeout=UPLOAD_TIMEOUT):
                pass
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", "replace")[:300]
            if error.code in (400, 409) and "taken" in detail.lower():
                return  # uploaded by a concurrent run between our check and our upload
            raise MirrorError(f"upload {path.name}: HTTP {error.code}: {detail}" + size_hint(error.code, detail, body.length)) from error
        except OSError as error:  # a proxy that closes the connection on a body over its limit
            raise MirrorError(f"upload {path.name}: {error}" + size_hint(0, "", body.length)) from error
        finally:
            body.close()
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


def bundle_stamp() -> str:
    now = time.time()  # milliseconds keep two runs in one second apart, and the names sort in time order
    return time.strftime("%Y%m%dT%H%M%S", time.gmtime(now)) + f"{int(now * 1000) % 1000:03d}Z"


def bundle_name(kind: str, stamp: Optional[str] = None) -> str:
    return f"pypi-{kind}-{stamp or bundle_stamp()}.tar"


def write_bundle(files: List[Path], out_dir: Path, kind: str, name: Optional[str] = None) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = bundle_stamp()
    path = out_dir / (name or bundle_name(kind, stamp))
    manifest = {"created": stamp, "kind": kind, "source": os.environ.get("CI_PROJECT_PATH", ""),
                "commit": os.environ.get("CI_COMMIT_SHA", ""),
                "files": [{"file": f.name, "sha256": sha256_file(f), "size": f.stat().st_size}
                          for f in sorted(files, key=lambda f: f.name)]}
    tmp = path.with_name("." + path.name + ".tmp")
    with tarfile.open(tmp, "w", format=tarfile.PAX_FORMAT) as tar:
        data = (json.dumps(manifest, indent=1) + "\n").encode()
        info = tarfile.TarInfo("MANIFEST.json")
        info.size, info.mtime = len(data), int(time.time())
        tar.addfile(info, fileobj=io.BytesIO(data))
        for f in sorted(files, key=lambda f: f.name):
            tar.add(f, arcname=f"wheels/{f.name}", recursive=False)
    os.replace(tmp, path)
    path.with_name(path.name + ".sha256").write_text(f"{sha256_file(path)}  {path.name}\n")
    total = sum(entry["size"] for entry in manifest["files"])
    progress(f"bundle: {path} ({len(files)} file(s), {total / 1e6:.1f} MB)")
    return path


PLAIN_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._+-]*")


def read_bundle(path: Path, into: Path) -> List[Tuple[Path, str]]:
    """Verify a bundle and its checksum, unpack it, and return (wheel, sha256) pairs."""
    sidecar = path.with_name(path.name + ".sha256")
    if sidecar.is_file() and sidecar.read_text().split()[0] != sha256_file(path):
        raise MirrorError(f"{path.name}: checksum does not match {sidecar.name}")
    with tarfile.open(path, "r:") as tar:
        for member in tar.getmembers():
            if not (member.isfile() or member.isdir()) or member.name.startswith("/") or ".." in Path(member.name).parts:
                raise MirrorError(f"{path.name}: refusing unsafe entry {member.name!r}")
        # The data filter (3.9.17 and later) also drops set-uid bits and refuses links and devices.
        tar.extractall(into, **({"filter": "data"} if hasattr(tarfile, "data_filter") else {}))
    manifest = json.loads((into / "MANIFEST.json").read_text())
    out = []
    for entry in manifest["files"]:
        if not PLAIN_NAME.fullmatch(str(entry.get("file", ""))):
            raise MirrorError(f"{path.name}: refusing manifest entry {entry.get('file')!r}")
        wheel = into / "wheels" / entry["file"]
        if not wheel.is_file() or sha256_file(wheel) != entry["sha256"]:
            raise MirrorError(f"{path.name}: {entry['file']} is missing or does not match its sha256")
        out.append((wheel, entry["sha256"]))
    return out


# --- sync: download -------------------------------------------------------------


def target_flags(target: Target) -> List[str]:
    """pip download flags that pick wheels for this target's Python and platform, not the host's."""
    flags = ["--only-binary=:all:", "--python-version", target.python, "--implementation", "cp"]
    for platform in target.platforms:
        flags += ["--platform", platform]
    return flags


def scratch() -> Path:
    """TMPDIR for pip and source builds: the staging directory sync was given, else the system's."""
    return STAGING or Path(tempfile.gettempdir())


MARKER_MACHINE = {"x86_64": "x86_64", "aarch64": "aarch64", "amd64": "AMD64", "arm64": "ARM64", "win32": "x86"}


def target_markers(target: Target) -> Dict[str, str]:
    """The environment-marker values pip on the target machine would see (PEP 508)."""
    windows = target.os == "windows"
    return {
        "os_name": "nt" if windows else "posix",
        "sys_platform": "win32" if windows else "linux",
        "platform_system": "Windows" if windows else "Linux",
        "platform_machine": MARKER_MACHINE[target.arch],
        "python_version": target.python,
        "python_full_version": f"{target.python}.0",
        "implementation_name": "cpython",
        "implementation_version": f"{target.python}.0",
        "platform_python_implementation": "CPython",
    }


# pip judges environment markers (platform_system == "Linux", python_version < "3.11") by
# the machine it runs on: --platform and --python-version change only which wheel tags fit.
# A Linux runner downloading for Windows then needs xgboost's Linux-only nvidia-nccl-cu12,
# which has no Windows wheel. This stub makes pip judge every marker as the target would.
PIP_AS_TARGET = """import json, runpy, sys
from pip._vendor.packaging import markers
target = json.loads(sys.argv.pop(1))
host = markers.default_environment
markers.default_environment = lambda: {**host(), **target}
sys.argv[0] = "pip"
runpy.run_module("pip", run_name="__main__", alter_sys=True)
"""


def pip_as(target: Target) -> List[str]:
    """The command that starts pip with the target's environment markers."""
    return [sys.executable, "-c", PIP_AS_TARGET, json.dumps(target_markers(target))]


def run_pip(args: List[str], stop_on: Optional[str] = None,
            target: Optional[Target] = None) -> Tuple[int, List[str]]:
    """Run this Python's pip, as the target when given, streaming its output to stderr.
    Returns its exit code and output lines."""
    pip = pip_as(target) if target else [sys.executable, "-m", "pip"]
    return run_streamed([*pip, "--disable-pip-version-check", *args], stop_on)


# This machine refused to open or run a file. Old pip (21.3, EL9's) then tries every older
# release, unpacking each sdist: pyspark's is 450 MB. The next one is refused the same way.
REFUSED = r"Operation not permitted|Permission denied"


NO_MATCH_RE = re.compile(r"No matching distribution found for (\S+)")
LINE_RE = re.compile(r"\(line (\d+)\)")


def stop_when_out_of_space(target: Target, out: List[str]) -> None:
    """A full disk is not a bad package: every later download would fail too, so stop."""
    if any("No space left on device" in line or "[Errno 28]" in line for line in out):
        raise MirrorError(
            f"{target.name}: no space left on device, in {WHEELHOUSE}/ or pip's temporary directory "
            f"{os.environ.get('TMPDIR') or scratch()}. Free space or pass --staging-dir on a larger disk, then run "
            f"sync again; the wheels already in {WHEELHOUSE}/ are kept and not fetched again.")


def last_error(out: List[str]) -> str:
    errors = [line.strip() for line in out if line.lstrip().startswith("ERROR:") or "Error:" in line]
    return errors[-1] if errors else (out[-1].strip() if out else "pip failed")


def owner(spec: str, pending: List[str], options: List[str], out: List[str]) -> Optional[str]:
    """The requirement line that pulls in this distribution: the line itself, or the line pip names."""
    name = req_name(spec)
    for line in pending:
        if req_name(line) == name:
            return line
    for line in reversed(out):  # "Collecting foo>=1 (from bar==2->-r file (line 3))"
        words = line.split()
        if len(words) > 1 and words[0] == "Collecting" and req_name(words[1]) == name:
            number = LINE_RE.search(line)
            if number:
                index = int(number[1]) - 1 - len(options)
                if 0 <= index < len(pending):
                    return pending[index]
            break
    return None


def pip_download(target: Target) -> List[str]:
    """Download every requirement of this target into the wheelhouse. Returns the lines it skipped."""
    requirements = target.path / "requirements.txt"
    if not (requirements.is_file() and read_lines(requirements)):
        return []
    wheelhouse().mkdir(exist_ok=True)
    lines = read_lines(requirements)
    options = [line for line in lines if line.startswith("-")]  # -i, --extra-index-url, -c ...
    pending = [line for line in lines if not line.startswith("-")]
    # Beside requirements.txt, so relative -r and -c paths resolve the same way.
    pending_file = target.path / PENDING
    try:
        skipped = resolve(target, pending, options, pending_file, set())
    finally:
        pending_file.unlink(missing_ok=True)
    for line, reason in skipped:
        warn(f"{target.name}: skipped {line}: {reason}\n  Fix: {fix_hint(target, line)}")
    return [line for line, _ in skipped]


def resolve(target: Target, pending: List[str], options: List[str], pending_file: Path,
            built: Set[str]) -> List[Tuple[str, str]]:
    """Download these lines as one pip run. A line pip cannot satisfy is built from its sdist or skipped,
    and the rest run again. Every file lands in the wheelhouse, so a rerun fetches nothing twice."""
    skipped: List[Tuple[str, str]] = []
    pending = list(pending)
    while pending:
        pending_file.write_text("\n".join(options + pending) + "\n")
        # Wheels only, for the target's Python and platform: a source distribution needs a
        # compiler and build dependencies on the air-gapped host. --find-links offers the
        # wheels already here, so a wheel built below or copied in by hand is used.
        command = ["download", "--dest", str(wheelhouse()), "--progress-bar", "off",
                   "--find-links", str(wheelhouse()), "-r", str(pending_file)] + target_flags(target)
        progress(f"==> {target.name}: {len(pending)} requirement(s): pip {' '.join(command)}")
        code, out = run_pip(command, target=target)
        if code == 0:
            return skipped
        stop_when_out_of_space(target, out)
        found = NO_MATCH_RE.findall("\n".join(out))
        spec = found[-1] if found else None
        line = owner(spec, pending, options, out) if spec else None
        if line is None and len(pending) == 1:
            line = pending[0]
        if line is None:
            warn(f"{target.name}: pip failed ({last_error(out)}); trying each requirement on its own")
            for one in pending:
                skipped += resolve(target, [one], options, pending_file, built)
            return skipped
        if spec and req_name(spec) not in built:
            built.add(req_name(spec))
            reason = build_from_sdist(target, spec)
            if reason is None:
                continue  # the built wheel is in the wheelhouse now; the same lines run again
        else:
            reason = last_error(out)
        if spec and req_name(spec) != req_name(line):
            reason = f"it needs {spec}, and {reason}"
        pending.remove(line)
        skipped.append((line, reason))
    return skipped


PENDING = ".requirements-pending.txt"
BARE_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


def req_name(text: str) -> str:
    """The canonical project name a requirement or pip spec starts with."""
    match = BARE_NAME_RE.match(text.strip())
    return canonical(match[0]) if match else text.strip()


def build_image(target: Target) -> str:
    return os.environ.get("MIRROR_BUILD_IMAGE", "python:{python}-slim").format(python=target.python)


def container_runtime() -> Optional[str]:
    """podman or docker on PATH; MIRROR_BUILD_RUNTIME=none builds on this machine instead."""
    wanted = os.environ.get("MIRROR_BUILD_RUNTIME", "")
    if wanted == "none":
        return None
    for name in ([wanted] if wanted else ["podman", "docker"]):
        if shutil.which(name):
            return name
    return None


PIP_PASSTHROUGH = ("PIP_INDEX_URL", "PIP_EXTRA_INDEX_URL", "PIP_TRUSTED_HOST",
                   "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "https_proxy", "http_proxy", "no_proxy")


def build_command(target: Target, spec: str, built: Path) -> Tuple[Optional[str], List[str]]:
    """How to build spec into built/: in a container of the target's Python when podman or docker
    is here, else with this machine's pip. Returns (runtime or None, command)."""
    runtime = container_runtime()
    if runtime:
        env = [arg for name in PIP_PASSTHROUGH if os.environ.get(name) for arg in ("-e", name)]
        return runtime, [runtime, "run", "--rm", "-v", f"{built}:/w:Z", *env, build_image(target),
                         "python", "-m", "pip", "wheel", "--disable-pip-version-check", "--no-deps",
                         "--progress-bar", "off", "--wheel-dir", "/w", spec]
    return None, []


def disk_bytes(*folders: Path) -> int:
    total = 0
    for folder in folders:
        for root, _, files in os.walk(folder):
            for name in files:
                try:
                    total += os.lstat(os.path.join(root, name)).st_size
                except OSError:
                    pass  # pip renames and removes its temporary files as it goes
    return total


SECRET_NAME = re.compile(r"TOKEN|PASSWORD|PASSWD|SECRET|JWT|CREDENTIAL|_KEY$|^AWS_|^VAULT_", re.IGNORECASE)


def run_streamed(command: List[str], stop_on: Optional[str] = None) -> Tuple[int, List[str]]:
    """Run a command, stream its output to stderr, and return its exit code and output lines.

    pip goes quiet while it downloads one large file or runs a build. After HEARTBEAT seconds
    without output, a line says how long it has run and how much has reached the disk.
    A line matching the stop_on pattern ends the command: retrying cannot help."""
    # pip runs setup.py of every sdist it builds: it sees no token of this job.
    env = {name: value for name, value in os.environ.items() if not SECRET_NAME.search(name)}
    env.setdefault("TMPDIR", str(scratch()))
    if "PIP_CACHE_DIR" not in env:
        # The wheelhouse is the cache: pip's own would hold a second copy of every
        # wheel in ~/.cache/pip, and torch with its CUDA wheels alone is gigabytes.
        env.setdefault("PIP_NO_CACHE_DIR", "1")
    lines: List[str] = []
    try:
        proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env,
                                text=True, errors="replace")
    except OSError as exc:
        return 127, [f"ERROR: {exc}"]
    queue: "queue_module.Queue[Optional[str]]" = queue_module.Queue()

    def read() -> None:
        for line in proc.stdout:
            queue.put(line)
        queue.put(None)

    threading.Thread(target=read, daemon=True).start()
    folders = [wheelhouse(), *([STAGING] if STAGING else [])]  # never walk all of the system's temp
    start, before = time.monotonic(), disk_bytes(*folders)
    with proc:
        while True:
            try:
                line = queue.get(timeout=HEARTBEAT)
            except queue_module.Empty:
                if not QUIET:
                    elapsed = int(time.monotonic() - start)
                    grown = max(0, disk_bytes(*folders) - before) // (1 << 20)
                    last = lines[-1].strip()[:120] if lines else command[0]
                    print(f"    ... still running after {elapsed // 60}m{elapsed % 60:02d}s, "
                          f"{grown} MB written so far; last: {last}", file=sys.stderr, flush=True)
                continue
            if line is None:
                break
            lines.append(line.rstrip("\n"))
            if not QUIET:
                sys.stderr.write(line)
                sys.stderr.flush()
            if stop_on and re.search(stop_on, line) and proc.poll() is None:
                proc.terminate()
    return proc.returncode, lines


def refused(out: List[str]) -> Optional[str]:
    """The line where this machine refused to open or run a file: fapolicyd, noexec or permissions."""
    lines = [line.strip() for line in out if re.search(REFUSED, line)]
    return lines[-1] if lines else None


def build_from_sdist(target: Target, spec: str) -> Optional[str]:
    """Build a wheel from the sdist of spec into the wheelhouse. None when it worked, else why not.

    Only a wheel the target can install is kept: a pure-Python build (py3-none-any)
    fits every target; a compiled one fits only the platform that built it."""
    name = req_name(spec)
    container_error = ""
    with tempfile.TemporaryDirectory(dir=scratch()) as tmp:
        src, built = Path(tmp) / "src", Path(tmp) / "built"
        built.mkdir()
        runtime, command = build_command(target, spec, built)
        if runtime:
            progress(f"==> {target.name}: no wheel of {spec} for {target.os} Python {target.python}; "
                     f"building one from its source distribution in {runtime} {build_image(target)}")
            code, out = run_streamed(command)
            if code != 0:
                stop_when_out_of_space(target, out)
                if NO_MATCH_RE.search("\n".join(out)):
                    return f"there is no wheel for {target.os} Python {target.python} and no source distribution"
                container_error = f"{runtime} {build_image(target)}: {last_error(out)}"
                warn(f"{target.name}: the build in {container_error}; trying this machine's pip")
                runtime = None
        if not runtime:
            progress(f"==> {target.name}: no wheel of {spec} for {target.os} Python {target.python}; "
                     f"building one from its source distribution with this machine's pip")
            # --no-binary for this package only, so its build dependencies (setuptools) still come
            # as wheels. --no-deps lets --python-version pick a release the target's Python supports.
            code, out = run_pip(["download", "--no-deps", "--no-binary", name, "--python-version", target.python,
                                 "--progress-bar", "off", "--dest", str(src), spec], stop_on=REFUSED)
            if code == 0:
                sdists = sorted(src.iterdir())
                if len(sdists) != 1:
                    return f"pip downloaded {len(sdists)} files for its source distribution, expected 1"
                # The download machine's Python may be older than the target's; a pure-Python build does not care.
                code, out = run_pip(["wheel", "--no-deps", "--ignore-requires-python", "--progress-bar", "off",
                                     "--wheel-dir", str(built), str(sdists[0])], stop_on=REFUSED)
            if code != 0:
                stop_when_out_of_space(target, out)
                if container_error:
                    return (f"there is no wheel, the build in {container_error}, "
                            f"and this machine's build failed too: {refused(out) or last_error(out)}")
                denied = refused(out)
                if denied:  # pip runs setup.py to read an sdist's metadata, before NO_MATCH below
                    return (f"there is no wheel, and this machine refused to run its build ({denied}); "
                            f"fapolicyd or a noexec mount blocks a package's setup code under "
                            f"{os.environ.get('TMPDIR') or scratch()}")
                if NO_MATCH_RE.search("\n".join(out)):
                    return f"there is no wheel for {target.os} Python {target.python} and no source distribution"
                return f"there is no wheel, and its source distribution did not build: {last_error(out)}"
        wheels = sorted(built.glob("*.whl"))
        if len(wheels) != 1:
            return f"its source distribution built {len(wheels)} wheels, expected 1"
        if not wheel_fits(wheels[0].name, target):
            return f"there is no wheel, and its source builds {wheels[0].name}, which does not install on {target.name}"
        shutil.copy2(wheels[0], wheelhouse() / wheels[0].name)
        progress(f"    built {wheels[0].name} into {WHEELHOUSE}/")
    return None


def fix_hint(target: Target, line: str) -> str:
    name = req_name(line)
    return (f"pin a release with a wheel for {target.os} Python {target.python} (https://pypi.org/project/{name}/#files); "
            f"or install podman or docker here and run sync again, which builds a pure-Python package in "
            f"{build_image(target)}; or build the wheel on a machine that allows it, for example "
            f"podman run --rm -v \"$PWD/{WHEELHOUSE}:/w:Z\" {build_image(target)} pip wheel --no-deps '{line}' -w /w , "
            f"and the next sync uses it from {WHEELHOUSE}/; "
            f"or remove the line from {(target.path / 'requirements.txt').relative_to(ROOT)}.")


def canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def download(targets: List[Target]) -> int:
    """pip download every target into the wheelhouse. 1 when every requirement was skipped."""
    skipped = {target.name: pip_download(target) for target in targets}
    skips = [f"{name}: {line}" for name, lines in skipped.items() for line in lines]
    if not skips:
        return 0
    # A package with no usable wheel is skipped, so one bad line does not stop the
    # sync. Every line skipped means pip reached nothing: an outage, not a package.
    wanted = sum(len([line for line in read_lines(t.path / "requirements.txt") if not line.startswith("-")])
                 for t in targets if (t.path / "requirements.txt").is_file())
    if len(skips) == wanted:
        fail(f"every requirement was skipped ({'; '.join(skips)}); check that pip can reach the index")
        return 1
    warn(f"{len(skips)} requirement(s) skipped, the rest downloaded: {'; '.join(skips)}")
    return 0


def publish(registry: "Registry", dry_run: bool, ledger: Optional["Ledger"] = None) -> Set[str]:
    """Upload every wheelhouse file the registry lacks. Returns the names uploaded (or that would be).

    With a ledger, the files about to be uploaded are dropped from it first: a run that uploads one and then fails
    before its request leaves it in the registry, and the next run asks for it only if the ledger does not say sent."""
    files = wheel_files()
    missing = [path for path in files if path.name not in registry.existing(name_and_version(path.name)[0])]
    if ledger and missing and not dry_run:
        ledger.forget({path.name for path in missing})
    uploaded: Set[str] = set()
    for path in missing:
        if dry_run:
            print(f"would upload {path.name}")
        else:
            registry.upload(path)
            progress(f"uploaded {path.name}")
        uploaded.add(path.name)
    verb = "to upload" if dry_run else "uploaded"
    progress(f"{len(files)} file(s): {len(uploaded)} {verb}, {len(files) - len(uploaded)} already in the registry")
    return uploaded


# --- request: ask the low NiFi to fetch each wheel itself -------------------------
#
# A bundle can exceed what the low NiFi accepts in one POST. A request is a small JSON
# list of wheels already in the registry; the low NiFi downloads each one from the
# registry and sends it across the link as its own FlowFile. Requests go in batches of
# MIRROR_REQUEST_BATCH wheels, and the ledger records each batch once NiFi takes it, so
# each wheel is asked for once and a failed batch, with every batch after it, is asked for again.

LEDGER_PACKAGE, LEDGER_VERSION, LEDGER_NAME = "pypi-mirror-ledger", "v1", "ledger.json"
REQUEST_TYPE = "python-packages-request"


class Ledger:
    """What request has sent the low NiFi: {file name: {sha256, time, pipeline}}. In a GitLab CI job (or with
    MIRROR_LEDGER=gitlab) a file in the project's generic packages, so nothing lives on the runner;
    else MIRROR_LEDGER or .mirror-ledger.json beside mirror.py."""

    def __init__(self, registry: "Registry"):
        setting = os.environ.get("MIRROR_LEDGER", "").strip()
        self.registry = registry
        self.in_gitlab = setting.lower() == "gitlab" or (not setting and bool(os.environ.get("CI_JOB_TOKEN")))
        self.path = None if self.in_gitlab else Path(setting or ROOT / ".mirror-ledger.json")
        self.url = f"{registry.project_api}/packages/generic/{LEDGER_PACKAGE}/{LEDGER_VERSION}/{LEDGER_NAME}"

    def _call(self, method: str, data: Optional[bytes] = None) -> Optional[bytes]:
        request = urllib.request.Request(self.url, data=data, method=method)
        # A job token goes as JOB-TOKEN. Any other goes as Basic auth: GitLab refuses a deploy token as PRIVATE-TOKEN.
        if self.registry.job_token:
            request.add_header("JOB-TOKEN", self.registry.token)
        else:
            request.add_header("Authorization", self.registry.auth)
        opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=self.registry.context), _NoRedirect())
        try:
            with opener.open(request, timeout=120) as response:
                return response.read()
        except urllib.error.HTTPError as error:
            if method == "GET" and error.code == 404:
                return None
            raise MirrorError(f"{method} {self.url}: HTTP {error.code}") from error
        except urllib.error.URLError as error:
            raise MirrorError(f"{method} {self.url}: {error.reason}") from error

    def where(self) -> str:
        return f"package {LEDGER_PACKAGE}/{LEDGER_VERSION}/{LEDGER_NAME}" if self.in_gitlab else str(self.path)

    def load(self) -> Dict[str, str]:
        raw = self._call("GET") if self.in_gitlab else (self.path.read_bytes() if self.path.is_file() else None)
        if raw is None:
            progress(f"ledger: none at {self.where()}, starting a new one")
            return {}
        data = json.loads(raw)
        if data.get("registry") != self.registry.base:
            raise MirrorError(f"the ledger at {self.where()} belongs to {data.get('registry')}, not "
                              f"{self.registry.base}; set MIRROR_LEDGER to another file")
        # Ledgers before sent kept {file name: sha256} under requested.
        older = {name: {"sha256": digest} for name, digest in data.get("requested", {}).items()}
        return {**older, **data.get("sent", {})}

    def save(self, sent: Dict[str, Dict[str, str]]) -> None:
        body = json.dumps({"registry": self.registry.base, "sent": sent}, indent=1, sort_keys=True).encode()
        if self.in_gitlab:
            # ponytail: GitLab keeps every upload of the file and serves the newest; prune old ones if they pile up.
            self._call("PUT", body)
        else:
            self.path.write_bytes(body)
        progress(f"ledger: {self.where()} records {len(sent)} sent file(s)")

    def forget(self, names: Set[str]) -> None:
        """Drop names from the ledger, so the next request asks for them whatever happens to this run."""
        sent = self.load()
        if names & sent.keys():
            self.save({name: entry for name, entry in sent.items() if name not in names})


def request_body(registry: "Registry", files: List[Path]) -> Dict[str, object]:
    """The JSON the low NiFi reads: one entry per wheel, with the URL it downloads it from and, when the wheel's
    METADATA has one, its Requires-Python, which the high side uploads with it."""
    entries = []
    for path in files:
        name, version = name_and_version(path.name)
        digest = sha256_file(path)
        entry = {"filename": path.name, "name": name, "version": version, "sha256": digest,
                 "size": path.stat().st_size, "url": f"{registry.base}/files/{digest}/{urllib.parse.quote(path.name)}"}
        requires_python = read_metadata(path).get("Requires-Python")
        if requires_python:
            entry["requires_python"] = requires_python
        entries.append(entry)
    return {"registry": registry.base, "files": entries}


# The same CI attributes as gitlab-registry-mirror's request, so one RouteOnAttribute or audit serves both.
CI_ATTRIBUTES = {"X-CI-Project": "CI_PROJECT_PATH", "X-CI-Pipeline-Url": "CI_PIPELINE_URL",
                 "X-CI-Commit-Sha": "CI_COMMIT_SHA"}


def header_value(value: object) -> str:
    """A value safe in an HTTP header and so a NiFi attribute: CR and LF removed."""
    return re.sub(r"[\r\n]", "", str(value)).strip()


def ci_attributes() -> Dict[str, str]:
    """The CI_* variables that are set and non-empty, as attributes; none outside GitLab CI."""
    found = {name: header_value(os.environ.get(variable, "")) for name, variable in CI_ATTRIBUTES.items()}
    return {name: value for name, value in found.items() if value}


def shown_url(url: str) -> str:
    """url with no user:password@, query or fragment, so a log never shows credentials or a token. The last @
    counts, so a password holding an unencoded / or @ is not printed either."""
    scheme, separator, rest = url.partition("://")
    if not separator:
        scheme, rest = "", url
    rest = re.split(r"[?#]", rest, maxsplit=1)[0]
    return f"{scheme}{separator}{rest.rpartition('@')[2]}"


def post_request(body: Dict[str, object], url: str) -> None:
    """POST the request JSON with the same attribute headers a bundle carries, so the low NiFi routes it."""
    data = json.dumps(body).encode()
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme == "https":
        context = ssl.create_default_context(
            cafile=os.environ.get("MIRROR_REQUEST_CA_BUNDLE") or os.environ.get("MIRROR_CA_BUNDLE") or None)
        conn = http.client.HTTPSConnection(parsed.hostname, parsed.port, timeout=60, context=context)
    else:
        conn = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=60)
    headers = {"Content-Type": "application/json", "Content-Length": str(len(data)),
               "Filename": f"pypi-request-{bundle_stamp()}.json", "X-Sha256": hashlib.sha256(data).hexdigest(),
               "X-Artifact-Type": REQUEST_TYPE, "X-Artifact-Format": "json", "X-Artifact-Action": ARTIFACT_ACTION,
               "X-Registry": header_value(body["registry"]), "X-File-Count": str(len(body["files"])),
               **ci_attributes()}
    token = os.environ.get("MIRROR_REQUEST_TOKEN", "").strip()
    if token:
        refuse_cleartext(url, "MIRROR_REQUEST_TOKEN")
        headers["Authorization"] = f"Bearer {token}"
    target = (parsed.path or "/") + (f"?{parsed.query}" if parsed.query else "")
    try:
        conn.request("POST", target, body=data, headers=headers)
        status = conn.getresponse().status
        progress(f"posted to NiFi: {headers['Filename']} -> {shown_url(url)} (HTTP {status}, {len(body['files'])} file(s), "
                 f"{len(data)} bytes)")
    except OSError as error:
        raise MirrorError(f"POST request to {shown_url(url)}: {error}") from error
    finally:
        conn.close()
    if not 200 <= status < 300:
        raise MirrorError(f"POST request to {shown_url(url)}: HTTP {status}")


def request(registry: "Registry", url: str, specs: List[str], every: bool, dry_run: bool,
            pending: Set[str]) -> int:
    """POST the wheels the ledger has not recorded as sent, or the ones named, to the low NiFi."""
    progress(f"MIRROR_REQUEST_URL is set: asking the low NiFi at {shown_url(url)} for "
             + ("every wheel again" if every else "the named wheels" if specs else "the wheels not yet sent"))
    ledger = Ledger(registry)
    sent = ledger.load()
    wanted = []
    for path in wheel_files():
        name, version = name_and_version(path.name)
        if specs:
            if not any(normalize(SPEC_RE.fullmatch(spec)[1]) == normalize(name) and (
                    not SPEC_RE.fullmatch(spec)[2]
                    or canonical_version(SPEC_RE.fullmatch(spec)[2]) == canonical_version(version)) for spec in specs):
                continue
        elif not every and path.name in sent and path.name not in pending:
            continue  # uploaded again this run: the low registry lost it, so the high one may have too
        if path.name not in registry.existing(name) and path.name not in pending:  # the low NiFi downloads it from there
            warn(f"{path.name} is not in the registry, not requested")
            continue
        wanted.append(path)
    if specs and not wanted:
        raise MirrorError(f"nothing in {WHEELHOUSE}/ and the registry matches {', '.join(specs)}")
    if not wanted:
        progress(f"nothing new to request: every wheel is in the ledger as sent ({len(sent)} file(s)), nothing posted to NiFi")
        return 0
    if dry_run:
        for path in wanted:
            print(f"would request {path.name}")
        print(f"would post {len(wanted)} file(s) to {shown_url(url)}")
        return 0
    # One POST per batch keeps each body under a proxy's size limit (nginx ingress allows 1 MB by default).
    size = int(os.environ.get("MIRROR_REQUEST_BATCH") or 200)
    batches = [wanted[start:start + size] for start in range(0, len(wanted), size)]
    for number, batch in enumerate(batches, 1):
        body = request_body(registry, batch)
        try:
            post_request(body, url)
        except MirrorError as error:
            left = len(wanted) - (number - 1) * size
            raise MirrorError(f"{error}; batch {number} of {len(batches)} failed, {left} file(s) not sent: "
                              "the ledger keeps the batches before it, and the next run asks for the rest") from error
        progress(f"requested {len(batch)} file(s) from the low NiFi (batch {number} of {len(batches)})")
        when = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        pipeline = os.environ.get("CI_PIPELINE_URL", "")
        sent.update({entry["filename"]: {"sha256": entry["sha256"], "time": when, "pipeline": pipeline}
                     for entry in body["files"]})
        ledger.save(sent)
    return 0


def resend_specs(args) -> Tuple[List[str], bool]:
    """What to ask for again whatever the ledger says: (NAME[==VERSION] specs, every wheel)."""
    every = args.resend == "all" or env_flag("MIRROR_RESEND_ALL")
    specs = parse_specs(os.environ.get("MIRROR_RESEND_PACKAGES", ""), "MIRROR_RESEND_PACKAGES")
    if args.resend and args.resend != "all":
        try:
            text = sys.stdin.read() if args.resend == "-" else Path(args.resend).read_text()
        except OSError as error:
            raise UsageError(f"--resend {args.resend}: {error.strerror}") from None
        specs += parse_specs(text, args.resend)
    return specs, every


def cmd_sync(args) -> int:
    global STAGING
    url = os.environ.get("MIRROR_REQUEST_URL", "").strip()
    if args.resend and not url:
        raise UsageError("--resend asks the low NiFi again: set MIRROR_REQUEST_URL")
    specs, every = resend_specs(args)
    registry = Registry.from_env()
    targets = find_targets()
    for target in targets:
        target.platforms  # noqa: B018 - reading validates platforms.txt and MIRROR_MANYLINUX
    given = args.staging_dir or os.environ.get("MIRROR_STAGING_DIR")
    if given:
        Path(given).mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="pypi-mirror-", dir=given or None) as staging:
        STAGING = Path(staging)
        try:
            if args.dry_run:
                progress(f"dry run: no download; planning from what is in {WHEELHOUSE}/ now")
            elif download(targets):
                return 1
            pending = publish(registry, args.dry_run, Ledger(registry) if url else None)
        finally:
            STAGING = None
    if not url:
        progress("MIRROR_REQUEST_URL is not set: nothing posted to NiFi, the ledger is unchanged")
        return 0
    return request(registry, url, specs, every, args.dry_run, pending)


def canonical_version(version: str) -> str:
    """A PEP 440 version in one spelling, so 2.3.4, 2.3.4.0 and 2.3.04 compare equal."""
    match = re.fullmatch(r"v?(\d+(?:\.\d+)*)(.*)", version.strip().lower())
    if not match:
        return version.strip().lower()
    release = [int(part) for part in match[1].split(".")]
    while len(release) > 1 and release[-1] == 0:
        release.pop()
    return ".".join(map(str, release)) + re.sub(r"[-_.]", "", match[2])


def wheel_fits(filename: str, target: "Target") -> bool:
    """Whether pip on this target's Python and platform could install the wheel, from its file name tags."""
    parts = filename[:-len(".whl")].split("-")
    if len(parts) < 5:
        return False
    pythons, abis, plats = (set(part.split(".")) for part in parts[-3:])
    major, minor = target.python.split(".")
    if not plats & (set(target.platforms) | {"any", f"{target.os}_{target.arch}"}):
        return False
    if pythons & {f"py{major}", f"py{major}{minor}"}:
        return True
    if f"cp{major}{minor}" in pythons and abis & {f"cp{major}{minor}", "none", "abi3"}:
        return True  # not cp312t (free-threaded) or cp312d (debug), which a regular build cannot load
    # abi3 wheels built for an older CPython 3 install on newer ones.
    return "abi3" in abis and any(re.fullmatch(rf"cp{major}(\d+)", tag) and int(tag[3:]) <= int(minor)
                                  for tag in pythons)


def parse_specs(text: str, source: str) -> List[str]:
    """NAME[==VERSION] lines, # comments and blank lines ignored."""
    specs = []
    for line in (line.split("#", 1)[0].strip() for line in text.splitlines()):
        if line:
            try:
                specs.append(package_spec(line))
            except argparse.ArgumentTypeError as error:
                raise UsageError(f"{source}: {error}") from None
    return specs


def read_specs(args) -> List[str]:
    """The packages to export: operands, then every -T FILE (- is stdin), one NAME[==VERSION] per line."""
    specs = list(args.packages)
    for source in args.files_from or []:
        specs += parse_specs(sys.stdin.read() if str(source) == "-" else Path(source).read_text(), str(source))
    return specs


def cmd_export(args) -> int:
    registry = Registry.from_env()
    targets = find_targets(args.target) if args.target else []
    specs = args.specs
    if specs:
        # Packages by hand, fresh: what the registry holds, whatever was sent before.
        wanted = []
        for spec in specs:
            match = SPEC_RE.fullmatch(spec)
            name, version = match[1], match[2]
            files = registry.package_files(name, version)
            if targets:
                files = [f for f in files if any(wheel_fits(f["file"], t) for t in targets)]
            if not files:
                raise MirrorError(f"the registry holds no wheel of {spec}"
                                  + (f" for {', '.join(t.name for t in targets)}" if targets else ""))
            wanted += files
        kind = "pick"
        progress(f"{len(wanted)} file(s) for {', '.join(specs)}"
                 + (f" on {', '.join(t.name for t in targets)}" if targets else ""))
    else:
        wanted = registry.files_since(args.since)
        if targets:
            wanted = [f for f in wanted if any(wheel_fits(f["file"], t) for t in targets)]
        kind = f"since-{args.since}"
        progress(f"{len(wanted)} file(s) received since {args.since}"
                 + (f" for {', '.join(t.name for t in targets)}" if targets else ""))
    wanted = list({f["file"]: f for f in wanted}.values())  # a wheel named twice is carried once
    to_stdout = args.output == Path("-")
    out_dir = Path(args.output).parent if args.output else Path(args.target_directory or ".")
    if args.dry_run:
        for item in wanted:
            print(f"would add {item['file']}")
        print(f"would write {'standard output' if to_stdout else out_dir / (Path(args.output).name if args.output else bundle_name(kind))}"
              f" ({len(wanted)} wheel(s))")
        return 0
    if to_stdout:
        out_dir = Path(tempfile.mkdtemp(prefix="pypi-mirror-"))  # the tar is built here, then streamed and removed
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(dir=out_dir, prefix=".download-") as tmp:  # beside the bundle, not /tmp
            paths = [registry.download(f["file"], f["sha256"], Path(tmp)) for f in wanted]
            bundle = write_bundle(paths, out_dir, kind, name=Path(args.output).name if args.output and not to_stdout else None)
        if to_stdout:
            with open(bundle, "rb") as stream:
                for chunk in iter(lambda: stream.read(1 << 20), b""):
                    sys.stdout.buffer.write(chunk)
            sys.stdout.buffer.flush()
            return 0
        print(bundle)
        return 0
    finally:
        if to_stdout:
            shutil.rmtree(out_dir, ignore_errors=True)


def inbox_tars(directory: Path) -> List[Path]:
    """Every tar in a directory, oldest first by the time in its name."""
    return sorted(directory.glob("*.tar"), key=lambda p: (p.name.rsplit("-", 1)[-1], p.name))


def cmd_import(args) -> int:
    registry = Registry.from_env()
    uploaded = skipped = 0
    bundles: List[Tuple[Path, bool]] = []  # (tar, from an inbox directory)
    for source in args.sources:
        if source.is_dir():
            bundles += [(tar, True) for tar in inbox_tars(source)]
        elif source.is_file():
            bundles.append((source, False))
        else:
            raise MirrorError(f"{source}: no such file or directory")
    with tempfile.TemporaryDirectory() as tmp:
        for number, (bundle, from_inbox) in enumerate(bundles):
            unpacked = Path(tmp) / str(number)
            wheels = read_bundle(bundle, unpacked)
            for wheel, _ in wheels:
                name, _ = name_and_version(wheel.name)
                if wheel.name in registry.existing(name):
                    skipped += 1
                    continue
                if args.dry_run:
                    print(f"would upload {wheel.name}")
                else:
                    registry.upload(wheel)
                    progress(f"uploaded {wheel.name}")
                uploaded += 1
            if from_inbox and not args.keep:
                for path in (bundle, bundle.with_name(bundle.name + ".sha256")):
                    if path.exists():
                        if args.dry_run:
                            print(f"would remove: {path}")
                        else:
                            path.unlink()
    verb = "to upload" if args.dry_run else "uploaded"
    progress(f"{uploaded + skipped} file(s): {uploaded} {verb}, {skipped} already in the registry")
    return 0


DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
SPEC_RE = re.compile(r"([A-Za-z0-9][A-Za-z0-9._-]*)(?:==([A-Za-z0-9._+!-]+))?")


def date_arg(text: str) -> str:
    if not DATE_RE.fullmatch(text):
        raise argparse.ArgumentTypeError(f"{text!r}: expected YYYY-MM-DD")
    return text


def package_spec(text: str) -> str:
    if not SPEC_RE.fullmatch(text.strip()):
        raise argparse.ArgumentTypeError(f"{text!r}: expected NAME or NAME==VERSION")
    return text.strip()




def build_parser() -> argparse.ArgumentParser:
    raw = argparse.RawDescriptionHelpFormatter
    parser = argparse.ArgumentParser(prog=prog(), description=__doc__, formatter_class=raw)
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    parser.add_argument("-q", "--quiet", action="store_true", help="print no progress on stderr; warnings, errors and data stay")
    parser.add_argument("--api-url", metavar="URL",
                        help="the GitLab API, such as https://gitlab.example.com/api/v4 "
                             "(or MIRROR_GITLAB_API_URL; default $CI_API_V4_URL)")
    parser.add_argument("--project", metavar="PATH|ID",
                        help="the project that holds the registry (or MIRROR_PROJECT; default $CI_PROJECT_ID)")
    sub = parser.add_subparsers(dest="command", required=True, metavar="command")

    def dry_flag(sub, does):
        sub.add_argument("-n", "--dry-run", action="store_true", help=f"{does} and change nothing")

    def command(name, handler, summary, examples, about=""):
        found = sub.add_parser(name, help=summary, description=summary, formatter_class=raw,
                               epilog=(about + "examples:\n" + examples).replace("{prog}", prog()))
        found.set_defaults(handler=handler)
        return found

    sync = command("sync", cmd_sync, "download every target, upload what the registry lacks, and ask the low NiFi for it",
                   "  {prog} sync\n  {prog} sync -n\n  {prog} sync --staging-dir /data/pypi-staging\n"
                   "  {prog} sync --resend all",
                   "which packages: every line of each packages/<target>/requirements.txt and all their dependencies,\n"
                   "as wheels for that target's Python and platform. A package with only an sdist is built into a wheel\n"
                   "in a podman or docker container of the target's Python (MIRROR_BUILD_IMAGE), else with this\n"
                   "machine's pip; one that still has no wheel is skipped with a warning that says how to fix it.\n"
                   "A misnamed target directory stops the run before anything is downloaded.\n"
                   "source: PyPI, or what pip reads from PIP_INDEX_URL, HTTPS_PROXY and PIP_CERT.\n"
                   "destination: every file in wheelhouse/ the registry lacks is uploaded, so a wheel you commit there\n"
                   "is published too. With MIRROR_REQUEST_URL, every wheel the ledger has not recorded as sent is\n"
                   "POSTed to the low NiFi as JSON in batches, and the ledger records each batch NiFi accepts.\n"
                   "staging: pip and source builds use a temporary directory under --staging-dir (or\n"
                   "MIRROR_STAGING_DIR, default the system's), removed when the run ends.\n"
                   "-n downloads nothing and prints 'would upload FILE', 'would request FILE' and 'would post'.\n"
                   "check it: pip index versions <name> --index-url <the registry's simple index>.\n\n")
    sync.add_argument("--staging-dir", metavar="DIR",
                      help="a large disk for pip and source builds (or MIRROR_STAGING_DIR; default the system's temp)")
    sync.add_argument("--resend", metavar="FILE|all",
                      help="ask NiFi again for the NAME[==VERSION] lines in FILE (- is stdin), or every wheel, "
                           "whatever the ledger says (or MIRROR_RESEND_PACKAGES, MIRROR_RESEND_ALL)")
    dry_flag(sync, "print what would be uploaded and requested")
    export = command("export", cmd_export, "tar every file the registry received since a date, or named packages",
                     "  {prog} export numpy==2.3.4 --target linux-py3.12 -o numpy.tar\n"
                     "  {prog} export -T packages.txt -t /mnt/usb\n"
                     "  {prog} export --since 2026-09-01 -t /mnt/usb\n"
                     "  {prog} export numpy==2.3.4 -o - | tee carry.tar | sha256sum",
                     "which packages: every wheel the registry holds for each PACKAGE (or -T line), at VERSION if given;\n"
                     "--target keeps only the wheels that target's Python and platform can install. Dependencies are not\n"
                     "added: name each one. With --since DATE instead, every wheel the registry received on or after that\n"
                     "day. Operands or -T and --since are exclusive; one is required. Needs a token with read_api\n"
                     "(MIRROR_TOKEN); a CI job token or a deploy token cannot list packages.\n"
                     "source: the registry named by --api-url and --project. Wheels are streamed to disk, never held whole\n"
                     "in memory, and each is checked against its sha256.\n"
                     "destination: pypi-pick-<UTC time>.tar (pypi-since-DATE.. for --since) and its .sha256 in the -t\n"
                     "directory (default .), or exactly -o FILE and FILE.sha256; the path is printed on stdout. -o - streams\n"
                     "the tar to stdout with no .sha256, built in a temporary directory as big as the tar.\n"
                     "check it: sha256sum -c TAR.sha256 ; tar -tf TAR ; -n prints 'would add FILE' and 'would write TAR'.\n\n")
    export.add_argument("packages", nargs="*", metavar="PACKAGE[==VERSION]", type=package_spec,
                        help="export these packages' wheels instead of a date range; dependencies are not added")
    export.add_argument("-T", "--files-from", type=Path, action="append", metavar="FILE",
                        help="a file of packages, one per line, # comments; - is stdin; repeat for more files")
    export.add_argument("--since", type=date_arg, default=os.environ.get("MIRROR_EXPORT_SINCE") or None,
                        help="YYYY-MM-DD, first day to include, instead of packages (or MIRROR_EXPORT_SINCE)")
    export.add_argument("--target", action="append", metavar="NAME",
                        help="keep only the wheels this target can install (repeatable)")
    where = export.add_mutually_exclusive_group()
    where.add_argument("-t", "--target-directory", metavar="DIR",
                       help="directory for the tar and its .sha256 (default: .)")
    where.add_argument("-o", "--output", type=Path, metavar="FILE",
                       help="write the tar to FILE, its .sha256 beside it; - writes the tar to stdout, with no .sha256 "
                            "(it needs temporary space as big as the tar)")
    dry_flag(export, "print the wheels and the tar it would write")
    imp = command("import", cmd_import, "verify tars and upload what the registry lacks; a directory is an inbox",
                  "  {prog} import numpy.tar\n  {prog} import /mnt/usb",
                  "which files: every wheel in each tar.\n"
                  "sources: a tar export wrote, or a directory, which is an inbox: every *.tar in it, oldest first by\n"
                  "the time in its name. Each inbox tar and its .sha256 are removed once imported (-k keeps them);\n"
                  "a tar that fails stays. A tar named on the command line is never removed.\n"
                  "checks: the .sha256 beside the tar if there is one, then every wheel against the manifest's sha256.\n"
                  "destination: the registry named by --api-url and --project. A wheel already there is skipped; a wheel\n"
                  "streams from disk, and a file over GitLab's PyPI package file size limit (default 5 GB) is refused by name.\n"
                  "-n prints 'would upload FILE' and 'would remove: PATH' and changes nothing.\n"
                  "check it: pip download <name>==<version> --index-url the registry's group simple index.\n\n")
    imp.add_argument("sources", nargs="*", type=Path, metavar="SOURCE",
                     help="a tar export wrote, or a directory (an inbox) of them")
    imp.add_argument("-k", "--keep", action="store_true", help="leave the tars of an inbox directory in place after importing")
    dry_flag(imp, "list what would be uploaded and removed")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "export":
        try:
            args.specs = read_specs(args)
        except (UsageError, OSError) as error:
            parser.error(str(error))
        if args.since and args.specs:
            parser.error("--since selects by date: give it or packages (operands, -T), not both")
        if not (args.since or args.specs):
            parser.error("export needs PACKAGE[==VERSION]..., -T FILE, or --since YYYY-MM-DD (or MIRROR_EXPORT_SINCE)")
    if args.command == "import" and not args.sources:
        parser.error("import needs a tar, or a directory of them")
    global QUIET
    QUIET = args.quiet
    if args.api_url:
        os.environ["MIRROR_GITLAB_API_URL"] = args.api_url
    if args.project:
        os.environ["MIRROR_PROJECT"] = args.project
    try:
        linux_platforms(manylinux(), "x86_64")
        env_flag("MIRROR_PUBLISH_SDISTS")  # a bad value fails here, before anything is uploaded
        return args.handler(args)
    except UsageError as error:
        parser.error(str(error))
    except MirrorError as error:
        fail(error)
        return 1


if __name__ == "__main__":
    sys.exit(main())
