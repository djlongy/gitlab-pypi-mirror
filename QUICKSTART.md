## What this is

`mirror.py` and `sync.sh` download Python packages per target (`packages/<os>-py<X.Y>/requirements.txt`) on a machine with PyPI access, and `.gitlab-ci.yml` uploads them to this project's GitLab PyPI registry, skipping files already there. It does not build packages from source or delete them from the registry.

## How to use it

1. Import this repository into your GitLab as a new project, then clone it on the machine with the proxy: `git clone https://gitlab.example.com/platform/pypi-mirror.git`
2. Create or edit a target file, for example `packages/linux-py3.12/requirements.txt`.
3. Check how each target is read: `python3 mirror.py targets`
4. Set the proxy, keeping GitLab direct: `export HTTPS_PROXY=http://proxy.example.com:3128 NO_PROXY=gitlab.example.com`
5. Download, commit and push: `./sync.sh`
6. Open **Build > Pipelines** and wait for the `publish` job.

You know it works when the `publish` log ends with `N file(s): N uploaded, 0 already in the registry`, and a rerun reports `0 uploaded`.

If it fails:
- `sync.sh` stops on git-lfs: `sudo dnf install git-lfs` (or `apt install git-lfs`), then `git lfs install`.
- pip cannot find a version for an old Python: pin an older release in that target's `requirements.txt`.
