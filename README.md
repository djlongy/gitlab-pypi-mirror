# gitlab-pypi-mirror

Syncs pip packages into a GitLab PyPI package registry. `pypi-mirror sync` downloads every package listed under `packages/`, builds a wheel when PyPI has only a source distribution, uploads what the registry lacks, records it in the ledger and posts a webhook to the low NiFi. When NiFi cannot carry it, `export` and `import` move wheels by hand.

## Requirements

- Python 3.8+ and pip, standard library only. Run it from a checkout of this repository, or link it onto your PATH: `ln -s "$PWD/mirror.py" ~/.local/bin/pypi-mirror`.
- A GitLab project with the package registry enabled.
- git-lfs while `.gitattributes` is present, to check out wheels committed to `wheelhouse/`.
- Optional: podman or docker, to build a wheel from an sdist in a container of the target's Python.

## Run from the CLI

The bare minimum for a sync:

```bash
export MIRROR_GITLAB_API_URL=https://gitlab.example.com/api/v4
export MIRROR_PROJECT=platform/pypi-mirror
read -rs MIRROR_TOKEN && export MIRROR_TOKEN
export MIRROR_REQUEST_URL=https://nifi.example.com:9310/pypi-request    # optional: the NiFi webhook
export HTTPS_PROXY=http://proxy.example.com:3128 NO_PROXY=gitlab.example.com   # optional
./mirror.py sync -n    # plan only: what would be uploaded and requested
./mirror.py sync
```

With a deploy token, also set `MIRROR_USERNAME` to the token's username. Without `MIRROR_REQUEST_URL`, sync uploads and stops: no webhook is posted and the ledger is not touched. `--staging-dir DIR` puts pip and source builds on a large disk; the default is the system's temp directory.

A target is a directory under `packages/` named `<os>-py<X.Y>[-<arch>]` holding a pip `requirements.txt`, and optionally `platforms.txt` (pip platform tags). A misnamed directory stops sync before it downloads anything.

| Directory | Wheels for |
|---|---|
| `linux-py3.12` | CPython 3.12, Linux x86_64, glibc 2.28 and older |
| `linux-py3.11-aarch64` | CPython 3.11, Linux aarch64 |
| `windows-py3.9` | CPython 3.9, Windows x64 |

Every target downloads into one `wheelhouse/`. pip judges environment markers as the target would. A package with only an sdist is built in podman or docker (`MIRROR_BUILD_IMAGE`), else with this machine's pip; one that still has no usable wheel is skipped with a warning naming the fix. A wheel you commit to `wheelhouse/` is published as is. Renovate (`renovate.json`) updates `==` pins, one MR per Python version. sync exits 1 when every requirement was skipped, which means pip reached nothing.

The webhook is small JSON POSTs of `MIRROR_REQUEST_BATCH` wheels, naming each wheel the registry holds that the ledger has not recorded as sent. The low NiFi downloads each wheel from the registry itself. The ledger records each batch NiFi accepts, with its time and pipeline, so a failed batch and every batch after it is asked for again next run. `--resend FILE` (or `MIRROR_RESEND_PACKAGES`) asks again for the `NAME[==VERSION]` lines given, `--resend all` (or `MIRROR_RESEND_ALL=true`) for every wheel.

## Run as a pipeline

1. **Settings > CI/CD > Variables**: `MIRROR_REQUEST_URL`, and `HTTPS_PROXY`/`NO_PROXY` if the runner reaches PyPI through a proxy. In CI the job token is the registry token and the ledger is the project's generic package `pypi-mirror-ledger/v1/ledger.json`.
2. **Build > Pipeline schedules > New schedule** on `main`, for example daily at 02:00. Or **Run pipeline** on `main` with `MIRROR_SYNC=true`.
3. A push to `main` runs sync too. Resend on Run pipeline with `MIRROR_RESEND_PACKAGES` or `MIRROR_RESEND_ALL=true`.

Jobs: `test` (skipped with `MIRROR_SKIP_TESTS=true`), `sync`, and `export` (Run pipeline with `MIRROR_EXPORT_SINCE=YYYY-MM-DD` and a masked `EXPORT_TOKEN`, Reporter, `read_api`). The sync job caches `wheelhouse/`, so pip fetches only new files.

## Fallback: export and import by hand

Low side, with a USB stick at `/media/usb`:

```bash
./mirror.py export six==1.17.0 --target linux-py3.12 -o /media/usb/six.tar   # one package
./mirror.py export -T wanted.txt -t /media/usb       # NAME[==VERSION] per line, - is stdin
./mirror.py export --since 2026-09-01 -t /media/usb  # every wheel received since a date
```

Each tar gets a `.sha256` beside it. Carry both. High side, with that side's three registry variables:

```bash
(cd /media/usb && sha256sum -c six.tar.sha256)
./mirror.py import /media/usb/six.tar
./mirror.py import /media/usb     # a directory is an inbox: every tar, oldest first, removed once imported
```

Dependencies are not added: export each one that is missing. `-o -` streams the tar to stdout. Wheels of any size stream in 1 MiB chunks and are checked against their sha256. GitLab refuses a PyPI file over its maximum PyPI package file size (default 5 GB), and a proxy can refuse less (nginx `client_max_body_size`): the error names both; raise the limit and import again.

## Tokens

| Command | Token |
|---|---|
| `sync` | Deploy token with `read_package_registry` and `write_package_registry`, a project token with `api`, or the CI job token |
| `import` | The same as `sync` |
| `export` | `read_api` (Reporter): a project or personal access token. A job token or deploy token cannot list packages |

## Commands

Global options go before the command: `--api-url URL`, `--project PATH|ID`, `-q`/`--quiet`, `--version`, `-h`/`--help`.

| Command | Operands | Options |
|---|---|---|
| `sync` | none | `--staging-dir DIR`, `--resend FILE\|all` (`-` is stdin, needs `MIRROR_REQUEST_URL`), `-n`/`--dry-run` |
| `export` | `[PACKAGE[==VERSION]...]` | `-T`/`--files-from FILE` (repeatable, `-` is stdin), `--since DATE` (not with packages), `--target NAME` (repeatable), `-t`/`--target-directory DIR` (default `.`) or `-o`/`--output FILE` (`-` is stdout), `-n`/`--dry-run` |
| `import` | `SOURCE...` (a tar, or a directory as inbox) | `-k`/`--keep`, `-n`/`--dry-run` |

Data goes to stdout (the plan lines of `-n`, each tar path) and progress to stderr.

### Exit codes

| Code | Meaning |
|---|---|
| 0 | Success |
| 1 | A failure: HTTP error, misnamed target, every requirement skipped, checksum mismatch |
| 2 | A usage error: bad option, missing source, `--since` with packages, `--resend` without `MIRROR_REQUEST_URL`, a bad boolean or `MIRROR_MANYLINUX` |

## Environment

| Req | Name | Default | Purpose |
|---|---|---|---|
| Yes | `MIRROR_GITLAB_API_URL` | `$CI_API_V4_URL` | `https://gitlab.example.com/api/v4`; the flag is `--api-url` |
| Yes | `MIRROR_PROJECT` | `$CI_PROJECT_ID` | Project path or id that holds the registry; the flag is `--project` |
| Yes | `MIRROR_TOKEN` | `$CI_JOB_TOKEN` | Registry token, see Tokens |
| Optional | `MIRROR_USERNAME` | `__token__` | The deploy token's username with a deploy token |
| Optional | `MIRROR_REQUEST_URL` | unset | Low NiFi ListenHTTP for the webhook; unset, sync posts nothing |
| Optional | `MIRROR_REQUEST_TOKEN`, `MIRROR_REQUEST_CA_BUNDLE` | unset, `MIRROR_CA_BUNDLE` | Sent as `Authorization: Bearer`, over https only; CA file for that URL |
| Optional | `MIRROR_REQUEST_BATCH` | `200` | Wheels per webhook POST |
| Optional | `MIRROR_LEDGER` | the generic package in CI, else `.mirror-ledger.json` | What was sent to NiFi, when and from which pipeline: a file, or `gitlab` |
| Optional | `MIRROR_RESEND_PACKAGES`, `MIRROR_RESEND_ALL` | unset | Request the `NAME[==VERSION]` lines given, or every wheel, whatever the ledger says |
| Optional | `MIRROR_WHEELHOUSE_DIR` | `wheelhouse/` | Where sync downloads wheels, on a large disk; the committed `wheelhouse/` is published too |
| Optional | `MIRROR_STAGING_DIR` | system temp | Default of `--staging-dir` |
| Optional | `MIRROR_CA_BUNDLE` | system store | CA file for GitLab and NiFi; it replaces the system store |
| Optional | `MIRROR_MANYLINUX` | `2_28` | Newest glibc your Linux hosts run, as `2_<minor>`, minor 17 or newer |
| Optional | `MIRROR_BUILD_RUNTIME` | `podman`, else `docker` | Runtime that builds a wheel from an sdist; `none` builds with this machine's pip |
| Optional | `MIRROR_BUILD_IMAGE` | `python:{python}-slim` | Build image, `{python}` is the target's version |
| Optional | `MIRROR_PUBLISH_SDISTS` | `false` | Also publish, request and export the sdists in `wheelhouse/` |
| Optional | `MIRROR_EXPORT_SINCE` | unset | Default of `--since`; in a pipeline it runs the `export` job |
| Optional | `MIRROR_SYNC`, `MIRROR_SKIP_TESTS` (CI) | unset | `true` runs sync on Run pipeline; `true` skips the `test` job |
| Optional | `PYTHON_IMAGE` (CI) | `python:3.12-slim` | Job image with python3 and pip |
| Optional | `HTTPS_PROXY`, `NO_PROXY`, `PIP_INDEX_URL`, `PIP_CERT` | unset | pip's own settings for reaching PyPI |

Booleans take `1 yes y true t on` or `0 no n false f off`, in any case; anything else is a usage error.

## Out of scope

- Removing packages from the registry, and scanning packages for vulnerabilities or licences.
