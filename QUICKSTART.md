## What this is

`mirror.py sync` downloads every package in `packages/<target>/requirements.txt` from PyPI, builds a wheel where only an sdist exists, uploads what the GitLab PyPI registry lacks, updates the ledger and posts a webhook to the low NiFi. `export` and `import` are the by-hand fallback.

## Run from the CLI

1. Add a package: `echo 'requests==2.32.5' >> packages/linux-py3.12/requirements.txt`
2. Set the bare minimum:
   ```bash
   export MIRROR_GITLAB_API_URL=https://gitlab.example.com/api/v4 MIRROR_PROJECT=platform/pypi-mirror
   read -rs MIRROR_TOKEN && export MIRROR_TOKEN     # deploy token: also set MIRROR_USERNAME
   export MIRROR_REQUEST_URL=https://nifi.example.com:9310/pypi-request   # optional webhook
   export HTTPS_PROXY=http://proxy.example.com:3128 NO_PROXY=gitlab.example.com   # optional
   ```
3. Plan, then run: `./mirror.py sync -n`, then `./mirror.py sync` (add `--staging-dir /big/disk` for large builds).

## Run as a pipeline

1. **Settings > CI/CD > Variables**: `MIRROR_REQUEST_URL`, plus `HTTPS_PROXY` and `NO_PROXY` if needed.
2. **Build > Pipeline schedules > New schedule** on `main`, or **Run pipeline** with `MIRROR_SYNC=true`.
3. To ask NiFi again: Run pipeline with `MIRROR_RESEND_PACKAGES` (lines of `NAME[==VERSION]`) or `MIRROR_RESEND_ALL=true`.

## Fallback by hand

1. Low side (token with `read_api`): `./mirror.py export six==1.17.0 --target linux-py3.12 -o /media/usb/six.tar`
2. High side: `sha256sum -c six.tar.sha256`, then `./mirror.py import /media/usb/six.tar`

You know it works when sync reports `N uploaded` on stderr and `pip install --index-url <group simple index> requests==2.32.5` succeeds.
