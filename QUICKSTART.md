## What this is

`mirror.py` downloads Python wheels per target (`packages/<os>-py<X.Y>/requirements.txt`) and uploads them to this project's GitLab PyPI registry, skipping files already there. A pipeline schedule does it unattended and keeps only the new files as a `pypi-delta-*.tar` artifact for 14 days, which `mirror.py import` loads into the high side's registry. It does not build packages from source or delete them from a registry.

## How to use it

1. Import this repository into your GitLab as a new project, then edit a target file such as `packages/linux-py3.12/requirements.txt`, leaving versions unpinned where you want updates.
2. Check how each target is read: `python3 mirror.py targets`
3. If the runner reaches PyPI through a proxy, add `HTTPS_PROXY` and `NO_PROXY` under **Settings > CI/CD > Variables**.
4. Create the schedule: **Build > Pipeline schedules > New schedule**, target branch `main`, for example daily at 02:00.
5. Run it once now: **Build > Pipelines > Run pipeline** on `main` with variable `MIRROR_SYNC` = `true`.
6. Download the `sync` job's artifact and carry `delta/pypi-delta-*.tar` and its `.sha256` to the high side.
7. On the high side, with `GITLAB_API_URL`, `PYPI_PROJECT` and `PYPI_TOKEN` set: `python3 mirror.py import pypi-delta-*.tar`

You know it works when the `sync` log ends with `bundle: delta/pypi-delta-...tar (N file(s), ...)` and the high-side import reports `N uploaded`.

If it fails:
- A bundle expired before it crossed: add a masked `PYPI_TOKEN` CI variable (project access token, Reporter, `read_api`), run the pipeline with `EXPORT_SINCE` = the first missed day, then import the `export` job's bundle.
- `sync` cannot reach PyPI: set the proxy variables in step 3, or add `tags:` for a runner with internet access to the `sync` job.
