## What this is

`mirror.py` (run as `pypi-mirror`) moves Python wheels into a GitLab PyPI package registry. By hand it exports one package for one Python version from the low-side registry as a tar, and imports that tar into the high-side registry. A pipeline schedule does the whole set unattended and keeps only the new files as a `pypi-delta-*.tar` artifact for 14 days. It does not build packages from source or delete them from a registry.

## How to use it

1. Put the tool on your PATH, from a checkout of this repository: `ln -s "$PWD/mirror.py" ~/.local/bin/pypi-mirror`
2. Low side, export one package: `MIRROR_GITLAB_API_URL=https://gitlab.example.com/api/v4 MIRROR_PROJECT=platform/pypi-mirror MIRROR_TOKEN=<read_api token> pypi-mirror export six==1.17.0 --target linux-py3.12 -o /media/usb/six.tar`
3. Carry `six.tar` and `six.tar.sha256`. High side: `sha256sum -c six.tar.sha256`, then `pypi-mirror import six.tar` with the high side's `MIRROR_GITLAB_API_URL`, `MIRROR_PROJECT` and a `write_registry` `MIRROR_TOKEN`.
4. For the whole set, edit a target file such as `packages/linux-py3.12/requirements.txt` and check how each target is read: `pypi-mirror check`
5. If the runner reaches PyPI through a proxy, add `HTTPS_PROXY` and `NO_PROXY` under **Settings > CI/CD > Variables**.
6. Create the schedule: **Build > Pipeline schedules > New schedule**, target branch `main`, for example daily at 02:00, or run it once: **Build > Pipelines > Run pipeline** on `main` with variable `MIRROR_SYNC` = `true`.
7. Download the `sync` job's artifact, carry `delta/pypi-delta-*.tar` to the high side, and run `pypi-mirror import delta --requirements packages`.

You know it works when `import` reports `N uploaded` on stderr and `pip install --index-url <group simple index> six==1.17.0` succeeds on the high side.

If it fails:
- A bundle expired before it crossed: add a masked `EXPORT_TOKEN` CI variable (project access token, Reporter, `read_api`), run the pipeline with `MIRROR_EXPORT_SINCE` = the first missed day, then import the `export` job's bundle. Too big for an artifact: `pypi-mirror export --since <day> -t /share/out` with the three registry variables set.
- A very large wheel is refused on import: the error names GitLab's PyPI package file size limit (default 5 GB) and any proxy limit; raise it and run `import` again.
- `sync` cannot reach PyPI: set the proxy variables in step 5, or add `tags:` for a runner with internet access to the `sync` job.
