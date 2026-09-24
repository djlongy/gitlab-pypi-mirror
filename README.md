# gitlab-pypi-mirror

Mirrors Python packages for chosen OS and Python versions into a GitLab PyPI package registry: `./sync.sh`.

## Requirements

- A download machine that reaches PyPI directly or through an HTTP proxy, with
  Python 3.8+, pip, git and git-lfs.
- GitLab with the package registry and Git LFS enabled on this project.
- A runner of any executor: docker runs `PYTHON_IMAGE`, shell needs `python3` 3.8+.
  The runner needs no internet access.

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
file in a `wheelhouse/` is ignored with a warning. Pin a release that has a wheel,
or build one elsewhere and add it to the `wheelhouse/`.

Optional in a target directory: `platforms.txt`, pip platform tags, one per line,
replacing the defaults.

Two targets that resolve the same file upload it once.

## Environment

The full set is in the argument parser and `Registry.from_env` in `mirror.py`.

| Req | Name | Default | Purpose |
|---|---|---|---|
| Optional | `HTTPS_PROXY`, `HTTP_PROXY` | none | Proxy pip uses to reach PyPI. Put the GitLab host in `NO_PROXY` |
| Optional | `PIP_INDEX_URL` | pypi.org | A PyPI mirror to download from instead |
| Optional | `PIP_CERT` | system store | CA bundle pip trusts, for a TLS-inspecting proxy |
| Optional | `MANYLINUX` | `2_28` | Newest glibc your Linux hosts run, as `2_<minor>` |
| When pruning | `GITLAB_API_URL` | `$CI_API_V4_URL` | `https://gitlab.example.com/api/v4` |
| When pruning | `PYPI_PROJECT` | `$CI_PROJECT_ID` | Project path or id that holds the registry |
| When pruning | `PYPI_TOKEN` | `$CI_JOB_TOKEN` | Token with `read_api`; `write_registry` too to publish from outside CI |
| Optional | `CA_BUNDLE` | system store | CA bundle for the GitLab API |
| Optional | `PYTHON_IMAGE` (CI) | `python:3.12-slim` | Job image; point it at your internal registry |

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

- `sync.sh` pulls, downloads every target into `packages/<target>/wheelhouse/`,
  commits, and pushes. `--no-push` stops after the commit; `--target` limits the run.
- With `GITLAB_API_URL`, `PYPI_PROJECT` and `PYPI_TOKEN` set, `sync.sh` first deletes
  downloaded files the registry already holds, so the push carries only new files.
- A push to the default branch that changes `packages/` runs `publish`, which asks
  the registry's simple index which files it holds and uploads only the rest.
  Running the pipeline from the web UI rechecks every file.
- `publish` never follows a redirect when checking. With package forwarding on (the
  GitLab default, under **Admin > Settings > CI/CD > Package Registry** and each
  group's **Packages and registries**), GitLab redirects a name it does not hold to
  pypi.org. Turn forwarding off so air-gapped clients fail fast instead of timing out.
- Each upload carries the wheel's `Requires-Python`, so pip on 3.9 never picks a
  release that needs 3.10.

## Out of scope

- Source distributions, and building wheels from them.
- Removing packages from the registry.
- Scanning packages for vulnerabilities or licences.

## Expected result

Every file in every `wheelhouse/` is in the project's package registry, and pip on
an air-gapped host installs from it:

```bash
pip install --index-url "https://__token__:$TOKEN@gitlab.example.com/api/v4/projects/<id>/packages/pypi/simple" requests==2.32.5
```
