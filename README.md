# gitlab-pypi-mirror

Moves Python wheels into a GitLab PyPI package registry, one package by hand or the whole set on a schedule: `pypi-mirror export six==1.17.0 --target linux-py3.12 -o six.tar`, carry it, `pypi-mirror import six.tar`.

## Requirements

- Python 3.8+ for `mirror.py`, standard library only. Put it on your PATH as `pypi-mirror`:
  `ln -s "$PWD/mirror.py" ~/.local/bin/pypi-mirror`. Run it from a checkout of this repository.
- GitLab with the package registry enabled on the project, on both sides of an air gap.
- `export` needs a token with `read_api` (Reporter). `import` and `publish` need `write_registry` (Developer).
- Download machine: pip, git, and git-lfs while `.gitattributes` is present. Any runner with `python3`, or with `curl`, `unzip` and `sha256sum` for `publish.sh`.

## Export one package by hand

Carry one package, for one Python version, from the low-side registry to the high-side registry.
The target `linux-py3.12` is the directory `packages/linux-py3.12`; its name says the Python and the platform.
Dependencies are not added: export each one that is missing too.

Low side, with the USB stick mounted at `/media/usb`:

```bash
export MIRROR_GITLAB_API_URL=https://gitlab.example.com/api/v4 MIRROR_PROJECT=platform/pypi-mirror
read -rs MIRROR_TOKEN && export MIRROR_TOKEN
pypi-mirror export six==1.17.0 --target linux-py3.12 -o /media/usb/six.tar
```

`/media/usb/six.tar` and `/media/usb/six.tar.sha256` are written. Carry both. High side:

```bash
export MIRROR_GITLAB_API_URL=https://gitlab.high.example.com/api/v4 MIRROR_PROJECT=platform/pypi-mirror
read -rs MIRROR_TOKEN && export MIRROR_TOKEN
(cd /media/usb && sha256sum -c six.tar.sha256)
pypi-mirror import /media/usb/six.tar
```

Add `-n` to either command to print the plan and change nothing.
`-o -` streams the tar to stdout for a pipe: `pypi-mirror export six==1.17.0 -o - | tee carry.tar | sha256sum`.

### Large wheels

The same two commands move a wheel of any size, over 1 GB included. `export` and `import` stream every file in 1 MiB chunks and never hold a wheel in memory. The tar needs free disk for the wheel, and `-o -` builds it in a temporary directory first.
GitLab refuses a PyPI file over its maximum PyPI package file size (default 5 GB, the `max_pypi_package_file_size` application setting), and a reverse proxy in front of GitLab can refuse a smaller body (nginx `client_max_body_size`). A refusal fails `import` with exit 1 and an error that names the file size and both limits. Raise the limit on the high-side instance and run `import` again: files already imported are skipped.

## Several packages

```bash
pypi-mirror export numpy==2.3.4 idna --target linux-py3.12 -t /media/usb    # name each package
pypi-mirror export -T wanted.txt -t /media/usb                             # one NAME[==VERSION] per line, # comments, - is stdin
pypi-mirror export --since 2026-09-01 -t /media/usb                        # every wheel the registry received since a date
pypi-mirror import /media/usb                                              # a directory is an inbox: every tar, then removed
```

`-t DIR` writes `pypi-pick-<UTC time>.tar` (`pypi-since-<date>-...` for `--since`) and its `.sha256` in `DIR`. `import` takes tars oldest first by name and removes each inbox tar and its `.sha256` once imported; `-k` keeps them.
`--requirements DIR` and `--git-bundle FILE` also write the requirement files and git history a bundle carries.

## Keep a set of packages in sync

Each directory under `packages/` is a target named `<os>-py<X.Y>[-<arch>]` with a pip `requirements.txt`, and an optional `platforms.txt` of pip platform tags.

| Directory | Wheels for |
|---|---|
| `linux-py3.12` | CPython 3.12, Linux x86_64, glibc 2.28 and older |
| `linux-py3.11-aarch64` | CPython 3.11, Linux aarch64 |
| `windows-py3.9` | CPython 3.9, Windows x64 |

```bash
echo 'requests==2.32.5' >> packages/linux-py3.12/requirements.txt
HTTPS_PROXY=http://proxy.example.com:3128 NO_PROXY=gitlab.example.com ./sync.sh
```

`sync.sh [TARGET...]` pulls, runs `download` into one `wheelhouse/`, commits and pushes; `--no-push` stops after the commit and `--prune` also runs `prune`.
Only wheels are mirrored, with every dependency. pip judges environment markers (`platform_system == "Linux"`, `python_version < "3.11"`) as the target, so a Linux runner downloading for Windows skips Linux-only dependencies such as xgboost's `nvidia-nccl-cu12`. A package that publishes only a source distribution, such as `pyspark`, is built into a wheel in a container of the target's Python (podman or docker), or with this machine's pip when neither is installed; a pure-Python build (`py3-none-any`) installs on every target, and a wheel you copy into `wheelhouse/` is used as is. A package that still has no usable wheel is skipped with a warning that names the fix, and the rest of the sync continues. A full disk stops the run. `download` exits 1 only when every line was skipped. Renovate updates `==` pins, one MR per Python version, and `pypi-mirror check` (the CI test job) fails when `renovate.json` does not hold a target to its Python. `pypi-mirror pin` pins bare names.

## Run it in a pipeline

A push to `main` that changes `packages/` or `wheelhouse/` runs `publish`: it asks the registry's simple index which files it holds and uploads the rest. `publish` never follows a redirect, so GitLab's forward-to-pypi.org setting cannot make a file look published; turn forwarding off so air-gapped clients fail fast.
A schedule, or Run pipeline with `MIRROR_SYNC=true`, runs `sync`: it downloads, publishes and writes only the uploaded files to `delta/pypi-delta-<UTC>.tar` with a `.sha256`, kept 14 days. Carry the tar and run `pypi-mirror import delta --requirements packages`.
After a missed bundle, run the pipeline with `MIRROR_EXPORT_SINCE=YYYY-MM-DD` and a masked `EXPORT_TOKEN` (project access token, Reporter, `read_api`); the `export` job bundles every file received since.
With `MIRROR_NIFI_URL` set, `sync` and `export` also POST each tar to a NiFi ListenHTTP with the headers `Filename`, `X-Sha256`, `X-Artifact-Type: python-packages`, `X-Artifact-Format: tar`, `X-Artifact-Action: mirror` and `X-Bundle-Kind`. `nifi/flow.py --side low|high` builds the transfer flow (the NiFi password comes from `--password-stdin` or `NIFI_PASSWORD`), and `nifi/pypi-to-gitlab.json` uploads each wheel to GitLab from NiFi. On the high side a timer runs `pypi-mirror import DIR`. Steps are in `nifi/PYPI-TO-GITLAB.md`.
With `MIRROR_REQUEST_URL` set, `sync` ends with `request`: small JSON POSTs, `MIRROR_REQUEST_BATCH` wheels each, naming every wheel the registry holds that the ledger has not recorded as sent. The low NiFi downloads each wheel from the registry itself and sends it across the link as its own FlowFile, so no POST carries a wheel and a wheel of any size crosses. The ledger is the generic package `pypi-mirror-ledger/v1/ledger.json` of this project; it records each batch NiFi accepts with its time and pipeline, so a failed batch, and every batch after it, is asked for again on the next run. `MIRROR_RESEND_PACKAGES` (lines of `NAME[==VERSION]`) and `MIRROR_RESEND_ALL=true` ask again on Run pipeline. `nifi/flow.py --side request-low|request-high` builds both flows (`nifi/pypi-request-low.json`, `nifi/pypi-request-high.json`); steps are in `nifi/PYPI-REQUEST.md`.

## Commands

Global options go before the command: `--api-url URL`, `--project PATH|ID`, `-q`/`--quiet`, `--version`, `-h`/`--help`. `pypi-mirror COMMAND -h` lists sources, destinations, outputs and checks.

| Command | Operands | Options |
|---|---|---|
| `list` | `[TARGET...]` | none |
| `check` | `[TARGET...]` | none |
| `pin` | `[PATH...]` (default `packages/`) | `-n`/`--dry-run` |
| `download` | `[TARGET...]` | none (pip reads `PIP_*`) |
| `publish` | none | `-t`/`--target-directory DIR`, `--nifi-url URL` (needs `-t`), `-n`/`--dry-run` |
| `prune` | none | `-n`/`--dry-run` |
| `export` | `[PACKAGE[==VERSION]...]` | `-T`/`--files-from FILE` (repeatable, `-` is stdin), `--since DATE` (not with packages), `--target NAME` (repeatable), `-t`/`--target-directory DIR` (default `.`) or `-o`/`--output FILE` (`-` is stdout), `--nifi-url URL`, `-n`/`--dry-run` |
| `request` | `[PACKAGE[==VERSION]...]` | `-T`/`--files-from FILE`, `--all`, `--request-url URL`, `-n`/`--dry-run` |
| `import` | `SOURCE...` (a tar, or a directory as inbox) | `-k`/`--keep`, `--requirements DIR`, `--git-bundle FILE`, `-n`/`--dry-run` |

`sync.sh`: `[TARGET...]`, `--no-push`, `--prune`, `-h`/`--help`. `publish.sh`: `-n`/`--dry-run`, `-q`/`--quiet`, `-h`/`--help`.
Data goes to stdout (`list`, the plan lines of `-n`, each tar path) and progress to stderr, so `tar=$(pypi-mirror export idna -t out)` works.

### Exit codes

| Code | Meaning |
|---|---|
| 0 | Success |
| 1 | A failure: HTTP error, unreadable bundle, checksum mismatch, no such target, a finding from `check` |
| 2 | A usage error: bad option, missing source, `--since` with packages, `--nifi-url` without `-t`, a bad boolean or `MIRROR_MANYLINUX` |

## Environment

The full set is in `build_parser` and `Registry.from_env` in `mirror.py`.

| Req | Name | Default | Purpose |
|---|---|---|---|
| With `--prune`, `publish`, `export`, `import` | `MIRROR_GITLAB_API_URL` | `$CI_API_V4_URL` | `https://gitlab.example.com/api/v4`; the flag is `--api-url` |
| Same | `MIRROR_PROJECT` | `$CI_PROJECT_ID` | Project path or id that holds the registry; the flag is `--project` |
| Same | `MIRROR_TOKEN` | `$CI_JOB_TOKEN` | Token: `read_api` to export, `write_registry` to publish and import |
| Optional | `MIRROR_USERNAME` | `__token__` | Registry user; `gitlab-ci-token` with a job token |
| Optional | `MIRROR_CA_BUNDLE` | system store | CA file for GitLab and NiFi; it replaces the system store, so it holds the full chain |
| Optional | `MIRROR_MANYLINUX` | `2_28` | Newest glibc your Linux hosts run, as `2_<minor>`, minor 17 or newer |
| Optional | `MIRROR_BUILD_RUNTIME` | `podman`, else `docker` | Container runtime that builds a wheel from an sdist; `none` builds with this machine's pip |
| Optional | `MIRROR_BUILD_IMAGE` | `python:{python}-slim` | Build image, `{python}` is the target's version; point it at your internal registry |
| Optional | `MIRROR_STAGING_DIR` | `.tmp/` beside `mirror.py` | `TMPDIR` for pip and source builds, on a large disk instead of the checkout; `download` removes what a killed run left there after 12 hours |
| Optional | `MIRROR_HEARTBEAT` | `15` | Seconds of pip silence before `download` prints elapsed time and MB written |
| Optional | `MIRROR_BUNDLE_REQUIREMENTS` | `true` | Bundles carry each target's `requirements.txt` and `platforms.txt` |
| Optional | `MIRROR_BUNDLE_GIT` | `false` | Bundles carry the git history; needs git in the job image and `GIT_DEPTH` 0 |
| Optional | `MIRROR_NIFI_URL` | unset | Default of `--nifi-url`, e.g. `https://nifi.example.com:9099/contentListener` |
| With mutual TLS | `MIRROR_NIFI_CLIENT_CERT`, `MIRROR_NIFI_CLIENT_KEY` | unset | Client certificate and key files for NiFi |
| With `request` | `MIRROR_REQUEST_URL` | unset | Low NiFi ListenHTTP for requests, e.g. `https://nifi.example.com:9310/pypi-request`; the flag is `--request-url` |
| Optional | `MIRROR_REQUEST_TOKEN`, `MIRROR_REQUEST_CA_BUNDLE` | unset, `MIRROR_CA_BUNDLE` | Sent as `Authorization: Bearer`; CA file for that URL |
| Optional | `MIRROR_REQUEST_BATCH` | `200` | Wheels per request POST; the ledger records each batch NiFi accepts |
| Optional | `MIRROR_LEDGER` | the project's generic package in CI, else `.mirror-ledger.json` | What `request` has sent, when and from which pipeline: a file, or `gitlab` |
| Optional | `MIRROR_RESEND_ALL`, `MIRROR_RESEND_PACKAGES` (CI) | unset | `request` every wheel, or the `NAME[==VERSION]` lines given |
| Optional | `MIRROR_EXPORT_SINCE` | unset | Default of `--since`; in a pipeline it runs the `export` job |
| Optional | `MIRROR_WHEELHOUSE_DIR` | `wheelhouse/` | Where `download` puts wheels, on a large disk; `publish` also uploads what is committed to `wheelhouse/` |
| Optional | `MIRROR_SYNC` (CI) | unset | `true` on Run pipeline runs `sync` now |
| Optional | `PYTHON_IMAGE` (CI) | `python:3.12-slim` | Job image with Python, or with curl, unzip and sha256sum |
| Optional | `HTTPS_PROXY`, `NO_PROXY`, `PIP_INDEX_URL`, `PIP_CERT` | unset | pip's own settings for reaching PyPI |

Booleans take `1 yes y true t on` or `0 no n false f off`, in any case; anything else is a usage error.

## Preconditions

- The download machine's git remote is this project, and it can push to `main`.
- A runner without git-lfs checks wheels out as pointer files, which `publish` refuses by name: delete `.gitattributes` and commit wheels as ordinary files.

## Out of scope

- Mirroring source distributions: a package without a wheel is built into one, never carried as an sdist.
- Removing packages from the registry.
- Scanning packages for vulnerabilities or licences.

## Expected result

Every wheel you exported is in the high-side project's registry, and pip on an air-gapped host installs it from the group index (`pip install --index-url https://gitlab.high.example.com/api/v4/groups/<group>/-/packages/pypi/simple six==1.17.0`; add `__token__:$TOKEN@` after `https://` for a private group). `pypi-mirror import -n SOURCE` lists nothing left to upload.
