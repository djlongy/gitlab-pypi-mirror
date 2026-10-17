#!/bin/sh
# Upload every wheel in wheelhouse/ that the GitLab PyPI registry
# does not already hold. The same job as `mirror.py publish`, for a runner with
# curl but no Python. Needs: curl, unzip, sha256sum (busybox has all three).
#
#   sh publish.sh             upload what is missing
#   sh publish.sh -n          print "would upload FILE" for what is missing, upload nothing
#   sh publish.sh -q          no progress on stderr; warnings and errors stay
#
# Progress goes to stderr and "would upload" lines to stdout, as mirror.py does.
# Exit 0 on success, 1 on a failure, 2 on a bad argument.
#
# Environment: the same as mirror.py. In CI nothing is needed; elsewhere set
# MIRROR_GITLAB_API_URL, MIRROR_PROJECT and MIRROR_TOKEN (MIRROR_USERNAME, default __token__).
# MIRROR_CA_BUNDLE is the CA file to trust for the GitLab API. It replaces the system
# store, so it must hold the whole chain GitLab's certificate needs.
set -eu

prog=${0##*/}
dry_run=0
quiet=0

usage() {
  echo "usage: $prog [-n|--dry-run] [-q|--quiet] [-h|--help]"
}
progress() { [ "$quiet" = 1 ] || echo "$*" >&2; }
warn() { echo "$prog: warning: $*" >&2; }
fail() { echo "$prog: $*" >&2; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    -n|--dry-run) dry_run=1 ;;
    -q|--quiet) quiet=1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "$prog: unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

api=${MIRROR_GITLAB_API_URL:-${CI_API_V4_URL:-}}
project=${MIRROR_PROJECT:-${CI_PROJECT_ID:-}}
user=${MIRROR_USERNAME:-__token__}
token=${MIRROR_TOKEN:-}
if [ -z "$token" ] && [ -n "${CI_JOB_TOKEN:-}" ]; then
  token=$CI_JOB_TOKEN
  user=gitlab-ci-token
fi
[ -n "$api" ] || fail "set MIRROR_GITLAB_API_URL (or run in CI)"
[ -n "$project" ] || fail "set MIRROR_PROJECT (or run in CI)"
[ -n "$token" ] || fail "set MIRROR_TOKEN (or run in CI)"
for tool in curl unzip sha256sum; do
  command -v "$tool" >/dev/null 2>&1 || fail "$tool is not installed"
done

# A project path goes in the URL encoded; a numeric id as it is.
case "$project" in
  *[!0-9]*) project=$(printf '%s' "$project" | sed 's|/|%2F|g') ;;
esac
base="${api%/}/projects/$project/packages/pypi"
wheelhouse=${MIRROR_WHEELHOUSE_DIR:-$(cd "$(dirname "$0")" && pwd)/wheelhouse}

# The token goes to curl on stdin as a config line, never on the command line.
curl_auth() {
  if [ -n "${MIRROR_CA_BUNDLE:-}" ]; then
    set -- --cacert "$MIRROR_CA_BUNDLE" "$@"
  fi
  printf 'user = "%s:%s"\n' "$user" "$token" | curl --silent --show-error --config - "$@"
}

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
total=0 uploaded=0 skipped=0

for path in "$wheelhouse"/*; do
  [ -f "$path" ] || continue
  file=${path##*/}
  case "$file" in
    *.whl) ;;
    *) warn "ignoring wheelhouse/$file: only wheels are published"; continue ;;
  esac
  if head -c 40 "$path" | grep -q '^version https://git-lfs'; then
    fail "$file is a Git LFS pointer, not a wheel. Install git-lfs where this runs, or delete .gitattributes and commit the wheels as ordinary files"
  fi
  total=$((total + 1))

  stem=${file%.whl}
  name=${stem%%-*}
  rest=${stem#*-}
  version=${rest%%-*}
  # PEP 503 name: runs of - _ . become one -, lower case.
  key=$(printf '%s' "$name" | tr 'A-Z' 'a-z' | sed 's/[-_.][-_.]*/-/g')

  # What the registry holds for this name, fetched once per name. No -L:
  # GitLab redirects a name it does not hold to pypi.org, and following that
  # would read PyPI's files as the registry's own.
  index="$work/index-$key"
  if [ ! -f "$index" ]; then
    # curl's own failure (DNS, TLS, refused) is HTTP 000 here, not a silent
    # exit from set -e; curl has already printed why.
    code=$(curl_auth --output "$work/page" --write-out '%{http_code}' "$base/simple/$key") || code=000
    case "$code" in
      200) grep -o '>[^<>]*</a>' "$work/page" | sed 's/^>//; s/<\/a>$//' > "$index" ;;
      404|3??) : > "$index" ;;
      *) fail "GET $base/simple/$key: HTTP $code" ;;
    esac
  fi
  if grep -qxF "$file" "$index"; then
    skipped=$((skipped + 1))
    continue
  fi

  if [ "$dry_run" = 1 ]; then
    echo "would upload $file"
    uploaded=$((uploaded + 1))
    continue
  fi
  requires=$(unzip -p "$path" '*.dist-info/METADATA' 2>/dev/null |
    sed -n 's/^Requires-Python: *//p' | tr -d '\r' | head -n 1)
  sha=$(sha256sum "$path" | cut -d' ' -f1)
  # --form-string, not --form: curl reads a --form value starting with < or @
  # as a file, and Requires-Python often starts with <.
  set -- --form-string "name=$name" --form-string "version=$version" \
    --form-string "sha256_digest=$sha"
  if [ -n "$requires" ]; then
    set -- "$@" --form-string "requires_python=$requires"
  fi
  : > "$work/reply"
  code=$(curl_auth --output "$work/reply" --write-out '%{http_code}' \
    "$@" --form "content=@$path" "$base") || code=000
  case "$code" in
    20?) progress "uploaded $file"; uploaded=$((uploaded + 1)); echo "$file" >> "$index" ;;
    400|409)
      if grep -qi taken "$work/reply"; then
        skipped=$((skipped + 1))  # uploaded by a concurrent run
      else
        fail "upload $file: HTTP $code: $(head -c 300 "$work/reply")"
      fi ;;
    *) fail "upload $file: HTTP $code: $(head -c 300 "$work/reply")" ;;
  esac
done

verb=uploaded
[ "$dry_run" = 1 ] && verb="to upload"
progress "$total file(s): $uploaded $verb, $skipped already in the registry"
