#!/usr/bin/env bash
# Download every target, commit and push. Wheels already in the repository
# stay; the push triggers the pipeline that uploads whatever the registry lacks.
#
# Run on the machine that can reach PyPI (directly or through HTTPS_PROXY).
#
#   ./sync.sh                         all targets, commit and push
#   ./sync.sh --target linux-py3.12   one target (repeatable)
#   ./sync.sh --no-push               download and commit only
#   ./sync.sh --prune                 also remove the repository's copy of every
#                                     wheel the registry already holds
#
# Environment:
#   HTTPS_PROXY / HTTP_PROXY  proxy for pip; put your GitLab host in NO_PROXY
#   PIP_INDEX_URL             a PyPI mirror to use instead of pypi.org
#   PIP_CERT                  CA bundle pip should trust (TLS-inspecting proxy)
#   PYTHON                    interpreter to run (default python3, needs 3.8+)
#   GITLAB_API_URL, PYPI_PROJECT, PYPI_TOKEN
#                             required with --prune only
set -euo pipefail
cd "$(dirname "$0")"

PYTHON=${PYTHON:-python3}
push=1
prune=0
args=()
while [ $# -gt 0 ]; do
  case "$1" in
    --no-push) push=0 ;;
    --prune) prune=1 ;;
    --target) args+=(--target "$2"); shift ;;
    -h|--help) sed -n '2,22p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done

if grep -q 'filter=lfs' .gitattributes 2>/dev/null && ! git lfs version >/dev/null 2>&1; then
  echo "ERROR: .gitattributes stores wheels in Git LFS and git-lfs is not installed." >&2
  echo "       Install git-lfs and run 'git lfs install', or delete .gitattributes" >&2
  echo "       to commit the files as ordinary blobs." >&2
  exit 1
fi

git pull --rebase --autostash
"$PYTHON" mirror.py download ${args[@]+"${args[@]}"}

if [ "$prune" = 1 ]; then
  "$PYTHON" mirror.py prune ${args[@]+"${args[@]}"}
fi

git add --all packages
if git diff --cached --quiet; then
  echo "nothing new to commit"
  exit 0
fi
git commit -q -m "Mirror packages $(date -u +%Y-%m-%d)" -m "$(git diff --cached --stat | tail -1)"
echo "committed: $(git log -1 --format=%s)"
if [ "$push" = 1 ]; then
  git push
fi
