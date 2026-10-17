#!/usr/bin/env python3
"""Mirror Python packages into a GitLab PyPI package registry.

Two halves, one file, standard library only:

  On a machine that can reach PyPI (directly or through an HTTP proxy):
      %(prog)s download      pip-download every packages/<target>/requirements.txt
      %(prog)s prune         remove local copies of wheels the registry already has

  In GitLab CI, where the runner cannot reach PyPI:
      %(prog)s publish       upload every downloaded file the registry lacks
                              (-t DIR: also tar just those files for the high side)

  Moving packages across an air gap:
      %(prog)s export        tar every file the registry received since a date
      %(prog)s import        verify a tar and upload what the registry here lacks

A target is a directory under packages/ named <os>-py<X.Y>[-<arch>]:

  packages/linux-py3.12/requirements.txt
  packages/windows-py3.9/requirements.txt
  packages/linux-py3.11-aarch64/requirements.txt

Every target downloads into one wheelhouse/ at the top of the repository. A
wheel's file name carries its Python and platform tags, so targets never
collide and a pure-Python wheel is stored once. Run `%(prog)s list` to see how
each directory name is read.

Python 3.8 or newer.
"""

from __future__ import annotations

import argparse
import base64
import email.parser
import fnmatch
import hashlib
import html
import http.client
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

VERSION = "0.1.0"
ROOT = Path(__file__).resolve().parent
PACKAGES = ROOT / "packages"
WHEELHOUSE = "wheelhouse"

TARGET_RE = re.compile(r"^(?P<os>linux|windows)-py(?P<py>3\.\d{1,2})(?:-(?P<arch>[a-z0-9_]+))?$")
DEFAULT_ARCH = {"linux": "x86_64", "windows": "amd64"}
ARCHES = {"linux": {"x86_64", "aarch64"}, "windows": {"amd64", "arm64", "win32"}}
# Newest glibc the Linux hosts have: 2_28 is RHEL 8 and later. pip does not
# expand a PEP 600 tag to the older ones, so linux_platforms() lists them all.
# X-Artifact-* on every NiFi POST, for RouteOnAttribute on a NiFi shared with other feeds.
ARTIFACT_TYPE = "python-packages"
ARTIFACT_FORMAT = "tar"
ARTIFACT_ACTION = "mirror"
LAST_BUNDLE = ".last-bundle"  # what the last bundle carried besides wheels, kept in the CI cache


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

    def definition(self) -> List[Path]:
        """The files that say what this target needs: requirements.txt and platforms.txt."""
        return [p for p in (self.path / "requirements.txt", self.path / "platforms.txt") if p.is_file()]


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


def renovate_gaps(targets: List[Target]) -> List[str]:
    """Targets renovate.json does not hold to their Python version. No renovate.json, no gaps."""
    path = ROOT / "renovate.json"
    if not path.is_file():
        return []
    config = json.loads(path.read_text())
    gaps = [] if config.get("constraintsFiltering") == "strict" else [
        'renovate.json: set "constraintsFiltering": "strict", or Renovate ignores the Python constraints'
    ]
    for target in targets:
        file = f"packages/{target.name}/requirements.txt"
        pythons = sorted({
            rule["constraints"]["python"] for rule in config.get("packageRules", [])
            if "python" in rule.get("constraints", {})
            and any(fnmatch.fnmatchcase(file, glob) for glob in rule.get("matchFileNames", []))
        })
        if pythons != [target.python]:
            gaps.append(
                f"packages/{target.name}: renovate.json gives Python {', '.join(pythons) or 'nothing'}, expected "
                f"{target.python}. Add to packageRules: "
                f'{{"groupName": "Python {target.python} packages", "matchFileNames": '
                f'["packages/*-py{target.python}/**", "packages/*-py{target.python}-*/**"], '
                f'"constraints": {{"python": "{target.python}"}}}}'
            )
    return gaps


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
        progress(f"moved {old.relative_to(ROOT)}/ into {WHEELHOUSE}/")


def wheel_files(dry_run: bool = False) -> List[Path]:
    """The wheels in the wheelhouse. Nothing else is ever published.

    A dry run leaves older per-target wheelhouses where they are and lists their files too."""
    if dry_run:
        folders = [wheelhouse(), *sorted(PACKAGES.glob(f"*/{WHEELHOUSE}"))]
        seen: Dict[str, Path] = {}
        for folder in folders:
            for path in (sorted(folder.iterdir()) if folder.is_dir() else []):
                if path.is_file():
                    seen.setdefault(path.name, path)
        files = sorted(seen.values())
    else:
        adopt_target_wheelhouses()
        files = sorted(p for p in wheelhouse().iterdir() if p.is_file()) if wheelhouse().is_dir() else []
    for path in files:
        if not path.name.endswith(".whl"):
            warn(f"ignoring {path.relative_to(ROOT)}: only wheels are published")
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
                    if item["created_at"][:10] >= since and item["file_name"].endswith(".whl"):
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
                          for item in self._files_of(package["id"]) if item["file_name"].endswith(".whl")]
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


def git(*args: str) -> str:
    # safe.directory: in CI the checkout belongs to another uid than the job.
    result = subprocess.run(["git", "-c", "safe.directory=*", "-C", str(ROOT), *args], capture_output=True, text=True)
    if result.returncode:
        raise MirrorError(f"git {args[0]}: {result.stderr.strip() or 'exit ' + str(result.returncode)}")
    return result.stdout.strip()


def extras() -> Tuple[List[Tuple[str, Path]], Optional[str]]:
    """The requirement files and the commit a bundle carries, per MIRROR_BUNDLE_REQUIREMENTS (default on) and
    MIRROR_BUNDLE_GIT (default off, needs git in the job image and a full clone: GIT_DEPTH 0)."""
    wanted = env_flag("MIRROR_BUNDLE_REQUIREMENTS", True)
    definitions = [(f"{t.name}/{p.name}", p) for t in find_targets() for p in t.definition()] if wanted else []
    head = None
    if env_flag("MIRROR_BUNDLE_GIT"):
        if not shutil.which("git"):
            raise MirrorError("MIRROR_BUNDLE_GIT is on and this job has no git: use an image with git, or turn MIRROR_BUNDLE_GIT off")
        if git("rev-parse", "--is-shallow-repository") != "false":
            raise MirrorError("MIRROR_BUNDLE_GIT is on and this is a shallow clone: set GIT_DEPTH to 0 for this job")
        head = git("rev-parse", "HEAD")
    return definitions, head


def fingerprint(definitions: List[Tuple[str, Path]], head: Optional[str]) -> str:
    return json.dumps([head] + [[name, sha256_file(p)] for name, p in definitions])


def bundle_stamp() -> str:
    now = time.time()  # milliseconds keep two runs in one second apart, and the names sort in time order
    return time.strftime("%Y%m%dT%H%M%S", time.gmtime(now)) + f"{int(now * 1000) % 1000:03d}Z"


def bundle_name(kind: str, stamp: Optional[str] = None) -> str:
    return f"pypi-{kind}-{stamp or bundle_stamp()}.tar"


def write_bundle(files: List[Path], out_dir: Path, kind: str,
                 definitions: List[Tuple[str, Path]], head: Optional[str], name: Optional[str] = None) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = bundle_stamp()
    path = out_dir / (name or bundle_name(kind, stamp))
    manifest = {"created": stamp, "kind": kind, "source": os.environ.get("CI_PROJECT_PATH", ""),
                "commit": os.environ.get("CI_COMMIT_SHA", "") or head or "",
                "files": [{"file": f.name, "sha256": sha256_file(f), "size": f.stat().st_size}
                          for f in sorted(files, key=lambda f: f.name)],
                "requirements": [{"file": name, "sha256": sha256_file(p)} for name, p in definitions]}
    work = tempfile.TemporaryDirectory()
    if head:
        repo = Path(work.name) / "repo.bundle"
        git("bundle", "create", str(repo), "HEAD")
        manifest["git"] = {"file": "repo.bundle", "sha256": sha256_file(repo), "head": head}
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
        if head:
            tar.add(repo, arcname="repo.bundle", recursive=False)
    work.cleanup()
    os.replace(tmp, path)
    path.with_name(path.name + ".sha256").write_text(f"{sha256_file(path)}  {path.name}\n")
    total = sum(entry["size"] for entry in manifest["files"])
    progress(f"bundle: {path} ({len(files)} file(s), {total / 1e6:.1f} MB{', git ' + head[:12] if head else ''})")
    return path


def post_bundle(path: Path, url: str, kind: str) -> None:
    """POST a bundle to NiFi's ListenHTTP, or anything else that takes a file in a POST body.

    The X- headers become flowfile attributes when ListenHTTP's "HTTP Headers for Attributes"
    regex matches them, named exactly as sent. http.client sends the names as written here;
    urllib would rewrite them."""
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme == "https":
        context = ssl.create_default_context(cafile=os.environ.get("MIRROR_CA_BUNDLE") or None)
        if os.environ.get("MIRROR_NIFI_CLIENT_CERT"):
            context.load_cert_chain(os.environ["MIRROR_NIFI_CLIENT_CERT"], os.environ.get("MIRROR_NIFI_CLIENT_KEY") or None)
        conn = http.client.HTTPSConnection(parsed.hostname, parsed.port, timeout=600, context=context)
    else:
        conn = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=600)
    headers = {"Content-Type": "application/x-tar", "Content-Length": str(path.stat().st_size),
               "Filename": path.name, "X-Sha256": sha256_file(path),
               "X-Artifact-Type": ARTIFACT_TYPE, "X-Artifact-Format": ARTIFACT_FORMAT,
               "X-Artifact-Action": ARTIFACT_ACTION, "X-Bundle-Kind": kind}
    target = (parsed.path or "/") + (f"?{parsed.query}" if parsed.query else "")
    try:
        with open(path, "rb") as body:
            conn.request("POST", target, body=body, headers=headers)
            status = conn.getresponse().status
    except OSError as error:
        raise MirrorError(f"POST {path.name} to {url}: {error}") from error
    finally:
        conn.close()
    if not 200 <= status < 300:
        raise MirrorError(f"POST {path.name} to {url}: HTTP {status}")
    progress(f"posted {path.name} to {url}")


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
    if "git" in manifest and sha256_file(into / "repo.bundle") != manifest["git"]["sha256"]:
        raise MirrorError(f"{path.name}: repo.bundle does not match its sha256")
    return out


# --- commands -----------------------------------------------------------------


def cmd_list(args) -> int:
    for target in find_targets(args.targets):
        requirements = target.path / "requirements.txt"
        print(
            f"{target.name:28} python {target.python:5} {target.os:8} {target.arch:8} "
            f"platforms={target.platforms[0]}{f' (+{len(target.platforms) - 1} older)' if len(target.platforms) > 1 else ''} "
            f"requirements={len(read_lines(requirements)) if requirements.is_file() else 0}"
        )
    return 0


def cmd_check(args) -> int:
    """Silent when every target is readable and renovate.json holds each to its Python; each gap on stderr, exit 1."""
    targets = find_targets(args.targets)
    for target in targets:
        target.platforms  # noqa: B018 - reading validates platforms.txt and MIRROR_MANYLINUX
    gaps = renovate_gaps(targets)
    for gap in gaps:
        fail(gap)
    return 1 if gaps else 0


def pip_download(target: Target) -> None:
    wheelhouse().mkdir(exist_ok=True)
    base = [sys.executable, "-m", "pip", "download", "--dest", str(wheelhouse()),
            "--disable-pip-version-check", "--progress-bar", "off"]
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
        progress(f"==> {target.name}: {' '.join(command[3:])}")
        subprocess.run(command, check=True)


BARE_RE = re.compile(r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)(?P<extras>\[[^\]]*\])?\s*(?P<comment>#.*)?$")


def canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def pip_resolve(target: Target, requirements: Path, names: List[str]) -> Dict[str, str]:
    """The version pip would download for each of these names, as {canonical name: version}."""
    # pip download, not pip install --dry-run --report: those need pip 22.2, and
    # EL9's stock pip is 21.3.1. The wheels land in a temporary directory only.
    with tempfile.TemporaryDirectory() as tmp:
        command = [sys.executable, "-m", "pip", "download", "--quiet", "--disable-pip-version-check",
                   "--dest", tmp, "-r", str(requirements), "--only-binary=:all:",
                   "--python-version", target.python, "--implementation", "cp"]
        for platform in target.platforms:
            command += ["--platform", platform]
        try:
            subprocess.run(command, check=True)
        except subprocess.CalledProcessError:
            raise MirrorError(f"{target.name}: pip could not resolve {requirements.relative_to(ROOT)}") from None
        found: Dict[str, Set[str]] = {}
        for wheel in Path(tmp).glob("*.whl"):
            name, version = name_and_version(wheel.name)
            found.setdefault(canonical(name), set()).add(version)
    wanted = {canonical(n) for n in names}
    missing = sorted(n for n in wanted if n not in found)
    if missing:
        raise MirrorError(f"{target.name}: pip downloaded no wheel for {', '.join(missing)}")
    several = sorted(f"{n} ({', '.join(sorted(found[n]))})" for n in wanted if len(found[n]) > 1)
    if several:
        raise MirrorError(f"{target.name}: pip downloaded more than one version of {', '.join(several)}")
    return {n: found[n].pop() for n in wanted}


def targets_in(paths: List[str]) -> List[Target]:
    """The targets whose requirements.txt is one of these paths or anywhere under them."""
    found: Dict[Path, Target] = {}
    for given in paths:
        path = Path(given).resolve()
        if not path.exists():
            raise MirrorError(f"{given}: no such file or directory")
        files = sorted(path.rglob("requirements.txt")) if path.is_dir() else [path]
        if not files or files[0].name != "requirements.txt":
            raise MirrorError(f"{given}: no requirements.txt here")
        for file in files:
            if file.parent not in found:
                found[file.parent] = Target(file.parent)
    return list(found.values())


def cmd_pin(args) -> int:
    """Pin every bare package name to the newest release this target can install. Pinned lines are left alone."""
    for target in targets_in(args.path or [str(PACKAGES)]):
        requirements = target.path / "requirements.txt"
        lines = requirements.read_text(encoding="utf-8-sig").splitlines() if requirements.is_file() else []
        bare = [i for i, line in enumerate(lines) if BARE_RE.match(line.strip())]
        if not bare:
            progress(f"{target.name}: nothing to pin")
            continue
        versions = pip_resolve(target, requirements, [BARE_RE.match(lines[i].strip())["name"] for i in bare])
        for i in bare:
            match = BARE_RE.match(lines[i].strip())
            pin = f"{match['name']}{match['extras'] or ''}=={versions[canonical(match['name'])]}"
            lines[i] = f"{pin}  {match['comment']}" if match["comment"] else pin
            if args.dry_run:
                print(f"would pin {target.name}: {pin}")
            else:
                progress(f"{target.name}: {pin}")
        if not args.dry_run:
            requirements.write_text("\n".join(lines) + "\n")
    return 0


def cmd_download(args) -> int:
    failed = []
    adopt_target_wheelhouses()
    for target in find_targets(args.targets):
        try:
            pip_download(target)
        except subprocess.CalledProcessError:
            failed.append(target.name)
            fail(f"pip download failed for {target.name}")
    if failed:
        fail(f"{len(failed)} target(s) failed: {', '.join(failed)}")
        return 1
    return 0


def nifi_target(args) -> Optional[str]:
    """Where to POST a bundle: --nifi-url, else MIRROR_NIFI_URL."""
    return args.nifi_url or os.environ.get("MIRROR_NIFI_URL") or None


def cmd_publish(args) -> int:
    registry = Registry.from_env()
    files = wheel_files(args.dry_run)
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
            progress(f"uploaded {filename}")
        uploaded.append(path)
    verb = "to upload" if args.dry_run else "uploaded"
    progress(f"{len(files)} file(s): {len(uploaded)} {verb}, {skipped} already in the registry")
    if args.target_directory:
        definitions, head = extras()
        state, marker = fingerprint(definitions, head), ROOT / LAST_BUNDLE
        carries_extras = bool(definitions or head)
        if args.dry_run:
            if uploaded or (carries_extras and (not marker.is_file() or marker.read_text() != state)):
                print(f"would bundle {len(uploaded)} file(s) into {Path(args.target_directory) / bundle_name('delta')}")
                if nifi_target(args):
                    print(f"would post to {nifi_target(args)}")
            else:
                progress("bundle: nothing new, no bundle would be written")
        elif uploaded or (carries_extras and (not marker.is_file() or marker.read_text() != state)):
            bundle = write_bundle(uploaded, Path(args.target_directory), "delta", definitions, head)
            print(bundle)
            if nifi_target(args):
                post_bundle(bundle, nifi_target(args), "delta")
            marker.write_text(state)  # after the POST, so a failed one is retried by the next run
        else:
            progress("bundle: nothing new, no bundle written")
    return 0


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


def read_specs(args) -> List[str]:
    """The packages to export: operands, then every -T FILE (- is stdin), one NAME[==VERSION] per line."""
    specs = list(args.packages)
    for source in args.files_from or []:
        text = sys.stdin.read() if str(source) == "-" else Path(source).read_text()
        for line in (line.split("#", 1)[0].strip() for line in text.splitlines()):
            if line:
                try:
                    specs.append(package_spec(line))
                except argparse.ArgumentTypeError as error:
                    raise UsageError(f"{source}: {error}") from None
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
        extra_files, head = extras()
        print(f"would write {'standard output' if to_stdout else out_dir / (Path(args.output).name if args.output else bundle_name(kind))}"
              f" ({len(wanted)} wheel(s), {len(extra_files)} requirements file(s){', git history' if head else ''})")
        if nifi_target(args) and not to_stdout:
            print(f"would post to {nifi_target(args)}")
        return 0
    if to_stdout:
        out_dir = Path(tempfile.mkdtemp(prefix="pypi-mirror-"))  # the tar is built here, then streamed and removed
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(dir=out_dir, prefix=".download-") as tmp:  # beside the bundle, not /tmp
            paths = [registry.download(f["file"], f["sha256"], Path(tmp)) for f in wanted]
            bundle = write_bundle(paths, out_dir, kind, *extras(), name=Path(args.output).name if args.output and not to_stdout else None)
        if to_stdout:
            with open(bundle, "rb") as stream:
                for chunk in iter(lambda: stream.read(1 << 20), b""):
                    sys.stdout.buffer.write(chunk)
            sys.stdout.buffer.flush()
            return 0
        print(bundle)
        if nifi_target(args):
            post_bundle(bundle, nifi_target(args), kind)
        return 0
    finally:
        if to_stdout:
            shutil.rmtree(out_dir, ignore_errors=True)


def inbox_tars(directory: Path) -> List[Path]:
    """Every tar in a directory, oldest first by the time in its name, so the newest requirements win."""
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
            if args.git_bundle and (unpacked / "repo.bundle").is_file():
                shutil.copyfile(unpacked / "repo.bundle", args.git_bundle)
                progress(f"git history from {bundle.name} written to {args.git_bundle}")
            if args.requirements and (unpacked / "requirements").is_dir():
                # Later bundles overwrite earlier ones, so pass them oldest first.
                shutil.copytree(unpacked / "requirements", args.requirements, dirs_exist_ok=True)
                progress(f"requirements from {bundle.name} written to {args.requirements}")
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


def cmd_prune(args) -> int:
    registry = Registry.from_env()
    removed = 0
    for path in wheel_files(args.dry_run):
        name, _ = name_and_version(path.name)
        if path.name in registry.existing(name):
            if args.dry_run:
                print(f"would remove: {path}")
            else:
                path.unlink()
            removed += 1
    progress(f"{'would remove' if args.dry_run else 'removed'} {removed} file(s) the registry already holds")
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

    list_ = command("list", cmd_list, "list the targets under packages/ and how each is read",
                    "  {prog} list\n  {prog} list linux-py3.12 windows-py3.9",
                    "which targets: every directory under packages/, or the TARGET operands.\n"
                    "output: one line per target on stdout (python, os, arch, first platform tag, requirement count).\n"
                    "check it: it changes nothing. An unreadable directory name or an unknown TARGET exits 1.\n\n")
    list_.add_argument("targets", nargs="*", metavar="TARGET", help="a target directory name under packages/ (default: all)")
    check = command("check", cmd_check, "fail when a target is misnamed or renovate.json does not hold it to its Python",
                    "  {prog} check\n  {prog} check linux-py3.12",
                    "which targets: every directory under packages/, or the TARGET operands.\n"
                    "what it checks: each directory name reads as <os>-py<X.Y>[-<arch>], platforms.txt parses, and\n"
                    "renovate.json holds each target to its Python (constraintsFiltering strict, one rule per version).\n"
                    "output: nothing on success. Each gap is one line on stderr, prefixed with the program name, and\n"
                    "the exit status is 1. The CI test job runs it.\n"
                    "check it: echo $? prints 0 when every target is readable and held to its Python.\n\n")
    check.add_argument("targets", nargs="*", metavar="TARGET", help="a target directory name under packages/ (default: all)")
    pin = command("pin", cmd_pin, "pin each unpinned requirement to the newest release its target can install",
                  "  {prog} pin\n  {prog} pin packages/linux-py3.12\n  {prog} pin -n",
                  "which requirements: every requirements.txt under the PATH operands (default packages/). Only bare\n"
                  "names are pinned; a line that already has a version is left alone.\n"
                  "how: pip resolves each target's Python and platform tags without installing, and the newest release it\n"
                  "can install becomes name==version. Dependencies are downloaded to a temporary directory, never pinned.\n"
                  "what it writes: the requirements.txt itself. -n prints 'would pin TARGET: name==version' and writes nothing.\n"
                  "check it: git diff packages/\n\n")
    pin.add_argument("path", nargs="*", metavar="PATH",
                     help="a target folder, a requirements.txt, or a folder holding targets (default: packages/)")
    dry_flag(pin, "print the pins it would write")
    download = command("download", cmd_download, "pip download every target into wheelhouse/",
                       "  {prog} download\n  {prog} download linux-py3.12",
                       "which packages: every line of each target's requirements.txt and all their dependencies, as\n"
                       "wheels only, for that target's Python and platform tags (pip --only-binary=:all:).\n"
                       "sources: PyPI, or what pip reads from PIP_INDEX_URL, HTTPS_PROXY and PIP_CERT.\n"
                       "what it writes: wheelhouse/, one directory for all targets. A wheel already there is not fetched again.\n"
                       "check it: ls wheelhouse/ ; a target with no wheel for a package fails and is named, exit 1.\n\n")
    download.add_argument("targets", nargs="*", metavar="TARGET", help="a target directory name under packages/ (default: all)")
    publish = command("publish", cmd_publish, "upload wheelhouse files the registry does not have",
                      "  {prog} publish\n  {prog} publish --dry-run\n  {prog} publish -t delta\n"
                      "  {prog} publish -t delta --nifi-url https://nifi.example.com:9099/contentListener",
                      "which files: every wheel in wheelhouse/ that the registry's simple index does not list. Nothing but\n"
                      "wheels is uploaded; a Git LFS pointer file is refused by name.\n"
                      "destination: the project registry from --api-url and --project (in CI, the job's own project).\n"
                      "what it writes: with -t, one pypi-delta-<UTC time>.tar and its .sha256 in DIR holding just the files\n"
                      "this run uploaded, plus each target's requirements.txt (MIRROR_BUNDLE_REQUIREMENTS) and the git history\n"
                      "(MIRROR_BUNDLE_GIT); its path is printed on stdout. A run with nothing new and unchanged extras writes none.\n"
                      "with --nifi-url (or MIRROR_NIFI_URL), the tar is also POSTed to a NiFi ListenHTTP.\n"
                      "-n prints 'would upload FILE', 'would bundle N file(s) into PATH' and 'would post to URL'.\n"
                      "check it: pip index versions <name> against the registry, or sha256sum -c TAR.sha256.\n"
                      "A file over GitLab's PyPI package file size limit (default 5 GB) is refused, and the error says so.\n\n")
    dry_flag(publish, "list what would be uploaded")
    publish.add_argument("-t", "--target-directory", metavar="DIR",
                         help="also write the files uploaded by this run to a tar in DIR")
    publish.add_argument("--nifi-url", metavar="URL",
                         help="with -t, also POST the tar to this NiFi ListenHTTP (or MIRROR_NIFI_URL)")
    prune = command("prune", cmd_prune, "remove local copies of wheels the registry already has",
                    "  {prog} prune\n  {prog} prune --dry-run",
                    "which files: wheels in wheelhouse/ that the registry's simple index lists. They are deleted here only;\n"
                    "nothing is removed from the registry.\n"
                    "output: -n prints 'would remove: PATH' for each file and deletes nothing.\n"
                    "check it: ls wheelhouse/ keeps only what the registry lacks.\n\n")
    dry_flag(prune, "print the files it would remove")
    export = command("export", cmd_export, "tar every file the registry received since a date, or named packages",
                     "  {prog} export numpy==2.3.4 --target linux-py3.12 -o numpy.tar\n"
                     "  {prog} export -T packages.txt -t /mnt/usb\n"
                     "  {prog} export --since 2026-09-01 -t /mnt/usb\n"
                     "  {prog} export numpy==2.3.4 -o - | tee carry.tar | sha256sum",
                     "which packages: every wheel the registry holds for each PACKAGE (or -T line), at VERSION if given;\n"
                     "--target keeps only the wheels that target's Python and platform can install. Dependencies are not\n"
                     "added: name each one. With --since DATE instead, every wheel the registry received on or after that\n"
                     "day. Operands or -T and --since are exclusive; one is required. Needs a token with read_api\n"
                     "(MIRROR_TOKEN); a CI job token cannot list packages.\n"
                     "source: the registry named by --api-url and --project. Wheels are streamed to disk, never held whole\n"
                     "in memory, and each is checked against its sha256.\n"
                     "what it writes: pypi-pick-<UTC time>.tar (pypi-since-DATE.. for --since) and its .sha256 in the -t\n"
                     "directory (default .), or exactly -o FILE and FILE.sha256; the path is printed on stdout. -o - streams\n"
                     "the tar to stdout with no .sha256, built in a temporary directory as big as the tar.\n"
                     "destination: also --nifi-url (or MIRROR_NIFI_URL) POSTs the tar to a NiFi ListenHTTP.\n"
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
    export.add_argument("--nifi-url", metavar="URL", help="also POST the tar to this NiFi ListenHTTP (or MIRROR_NIFI_URL)")
    dry_flag(export, "print the wheels and the tar it would write")
    imp = command("import", cmd_import, "verify tars and upload what the registry lacks; a directory is an inbox",
                  "  {prog} import numpy.tar\n  {prog} import /mnt/diode/received\n"
                  "  {prog} import /mnt/diode/received --requirements packages",
                  "sources: a tar publish or export wrote, or a directory, which is an inbox: every *.tar in it, oldest\n"
                  "first by the time in its name. Each inbox tar and its .sha256 are removed once imported (-k keeps them);\n"
                  "a tar that fails stays. A tar named on the command line is never removed.\n"
                  "checks: the .sha256 beside the tar if there is one, then every wheel against the manifest's sha256.\n"
                  "destination: the registry named by --api-url and --project. A wheel already there is skipped; a wheel\n"
                  "streams from disk, and a file over GitLab's PyPI package file size limit (default 5 GB) is refused by name.\n"
                  "what else it writes: --requirements DIR gets DIR/<target>/requirements.txt and platforms.txt; --git-bundle FILE\n"
                  "gets the git history. Pass tars oldest first so the newest requirements win.\n"
                  "-n prints 'would upload FILE' and 'would remove: PATH' and changes nothing.\n"
                  "check it: pip download <name>==<version> --index-url the registry's group simple index.\n\n")
    imp.add_argument("sources", nargs="*", type=Path, metavar="SOURCE",
                     help="a tar publish or export wrote, or a directory (an inbox) of them")
    imp.add_argument("-k", "--keep", action="store_true", help="leave the tars of an inbox directory in place after importing")
    imp.add_argument("--requirements", metavar="DIR",
                     help="also write the bundled requirements.txt and platforms.txt to DIR/<target>/")
    imp.add_argument("--git-bundle", metavar="FILE", help="also write the bundled git history to FILE")
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
    if args.command == "export" and args.output == Path("-") and args.nifi_url:
        parser.error("-o - writes the tar to stdout: it cannot also go to --nifi-url")
    if args.command == "publish" and args.nifi_url and not args.target_directory:
        parser.error("--nifi-url sends the tar publish writes: add -t DIR")
    global QUIET
    QUIET = args.quiet
    if args.api_url:
        os.environ["MIRROR_GITLAB_API_URL"] = args.api_url
    if args.project:
        os.environ["MIRROR_PROJECT"] = args.project
    try:
        linux_platforms(manylinux(), "x86_64")
        env_flag("MIRROR_BUNDLE_REQUIREMENTS", True)
        env_flag("MIRROR_BUNDLE_GIT")  # a bad value fails here, before anything is uploaded
        return args.handler(args)
    except UsageError as error:
        parser.error(str(error))
    except MirrorError as error:
        fail(error)
        return 1


if __name__ == "__main__":
    sys.exit(main())
