# gitlab-pypi-mirror

Mirrors Python packages for chosen OS and Python versions into a GitLab PyPI package registry: `./sync.sh`.

## Requirements

- A download machine that reaches PyPI directly or through an HTTP proxy, with
  Python 3.8+, pip, git and git-lfs.
- GitLab with the package registry and Git LFS enabled on this project.
- A runner of any executor, with `python3` 3.8+ or with `curl`, `unzip` and
  `sha256sum` (any busybox image). Without Python the pipeline publishes with
  `publish.sh`. Only the scheduled `sync` job needs PyPI access, and Python with pip.

## Targets

Each directory under `packages/` is one target, named `<os>-py<X.Y>[-<arch>]`, and
holds a `requirements.txt` in pip syntax. `python3 mirror.py targets` prints how each
name is read.

| Directory | Downloads wheels for |
|---|---|
| `linux-py3.12` | CPython 3.12, Linux x86_64, glibc 2.28 and older (`manylinux_2_28` down to `manylinux1`) |
| `linux-py3.11-aarch64` | CPython 3.11, Linux aarch64 |
| `windows-py3.9` | CPython 3.9, Windows x64 (`win_amd64`) |
| `windows-py3.13-arm64` | CPython 3.13, Windows on Arm |

Only wheels are downloaded and published, for every package and every dependency.
A package with no wheel for a target fails that target's download, and a non-wheel
file in `wheelhouse/` is ignored with a warning. Pin a release that has a wheel,
or build one elsewhere and add it to `wheelhouse/`.

Optional in a target directory: `platforms.txt`, pip platform tags, one per line,
replacing the defaults.

Every target downloads into one `wheelhouse/` at the repository root. A wheel's file
name carries its Python and platform tags, so a pure-Python wheel two targets need is
stored, downloaded and uploaded once. Wheels in an older `packages/<target>/wheelhouse/`
move there on the next `download` or `publish`.

## Environment

The full set is in the argument parser and `Registry.from_env` in `mirror.py`.

| Req | Name | Default | Purpose |
|---|---|---|---|
| Optional | `HTTPS_PROXY`, `HTTP_PROXY` | none | Proxy pip uses to reach PyPI. Put the GitLab host in `NO_PROXY` |
| Optional | `PIP_INDEX_URL` | pypi.org | A PyPI mirror to download from instead |
| Optional | `PIP_CERT` | system store | CA bundle pip trusts, for a TLS-inspecting proxy |
| Optional | `MANYLINUX` | `2_28` | Newest glibc your Linux hosts run, as `2_<minor>` |
| With `--prune` | `GITLAB_API_URL` | `$CI_API_V4_URL` | `https://gitlab.example.com/api/v4` |
| With `--prune` | `PYPI_PROJECT` | `$CI_PROJECT_ID` | Project path or id that holds the registry |
| With `--prune` | `PYPI_TOKEN` | `$CI_JOB_TOKEN` | Token with `read_api`; `write_registry` too to publish from outside CI |
| Optional | `CA_BUNDLE` | system store | CA file for the GitLab API. Replaces the system store, so it holds the full chain |
| Optional | `PYTHON_IMAGE` (CI) | `python:3.12-slim` | Job image with Python, or with curl, unzip and sha256sum; point it at your internal registry |
| Optional | `MIRROR_SYNC` (CI) | unset | `true` on Run pipeline runs the scheduled `sync` job now |
| Optional | `EXPORT_SINCE` (CI) | unset | `YYYY-MM-DD` on Run pipeline: bundle every file the registry received since then. Needs a masked CI variable `EXPORT_TOKEN`: a project access token, Reporter, `read_api` |

## Usage

```bash
git clone https://gitlab.example.com/platform/pypi-mirror.git
cd pypi-mirror
echo 'requests==2.32.5' >> packages/linux-py3.12/requirements.txt
HTTPS_PROXY=http://proxy.example.com:3128 NO_PROXY=gitlab.example.com ./sync.sh
```

## Preconditions

- The download machine's git remote is this project, and it can push to `main`.

## Behaviour

- `sync.sh` pulls, downloads every target into `wheelhouse/`, commits, and pushes.
  `--no-push` stops after the commit; `--target` limits the download.
- Every wheel stays in the repository, so it is also the archive the registry can
  be rebuilt from. A rerun downloads only files not already in `wheelhouse/`.
- `sync.sh --prune` instead removes the repository's copy of each wheel the registry
  already holds, keeping the repository small. It needs `GITLAB_API_URL`,
  `PYPI_PROJECT` and `PYPI_TOKEN`, and never removes anything from the registry.
- A push to the default branch that changes `packages/` or `wheelhouse/` runs `publish`, which asks
  the registry's simple index which files it holds and uploads only the rest.
  Running the pipeline from the web UI rechecks every file.
- `publish` never follows a redirect when checking. With package forwarding on (the
  GitLab default, under **Admin > Settings > CI/CD > Package Registry** and each
  group's **Packages and registries**), GitLab redirects a name it does not hold to
  pypi.org. Turn forwarding off so air-gapped clients fail fast instead of timing out.
- Each upload carries the wheel's `Requires-Python`, so pip on 3.9 never picks a
  release that needs 3.10.
- A pipeline schedule on the default branch runs `sync` instead of `publish`: it downloads
  in the job, publishes, and writes only the files this run uploaded to
  `delta/pypi-delta-<UTC>.tar` with a `.sha256`, kept as an artifact for 14 days. The
  tar also holds every target's `requirements.txt` and `platforms.txt` under
  `requirements/<target>/`, and `MANIFEST.json` names the commit. A run that uploads
  nothing writes no bundle. Unpinned requirements pick up new releases.
- On the high side, `python3 mirror.py import pypi-*.tar` checks the checksum and each
  wheel's sha256, then uploads what that registry lacks. `--requirements DIR` also
  writes the bundled target files to `DIR/<target>/`; pass bundles oldest first so the
  newest wins. After a missed or expired
  bundle, run the pipeline with `EXPORT_SINCE` and import the `export` job's bundle.

## Out of scope

- Source distributions, and building wheels from them.
- Removing packages from the registry.
- Scanning packages for vulnerabilities or licences.

## Expected result

Every file in `wheelhouse/` is in the project's package registry, and pip on
an air-gapped host installs from the group index, which serves every project in the
group. With a public group and a public project, pip needs no token. The group is
its id or its URL-encoded path. Uploads always go to the project.

```bash
pip install --index-url "https://gitlab.example.com/api/v4/groups/<group>/-/packages/pypi/simple" requests==2.32.5
```

For a private group, add `__token__:$TOKEN@` after `https://`, with a token that
has `read_api` on the group.
