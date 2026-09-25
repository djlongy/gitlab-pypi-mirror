#!/bin/sh
# Upload every wheel in wheelhouse/ that the GitLab PyPI registry
# does not already hold. The same job as `mirror.py publish`, for a runner with
# curl but no Python. Needs: curl, unzip, sha256sum (busybox has all three).
#
#   sh publish.sh             upload what is missing
#   sh publish.sh --dry-run   list what would be uploaded
#
# Environment: the same as mirror.py. In CI nothing is needed; elsewhere set
# GITLAB_API_URL, PYPI_PROJECT and PYPI_TOKEN (PYPI_USERNAME, default __token__).
# CA_BUNDLE is the CA file to trust for the GitLab API. It replaces the system
# store, so it must hold the whole chain GitLab's certificate needs.
set -eu

dry_run=0
[ "${1:-}" = "--dry-run" ] && dry_run=1

api=${GITLAB_API_URL:-${CI_API_V4_URL:-}}
project=${PYPI_PROJECT:-${CI_PROJECT_ID:-}}
user=${PYPI_USERNAME:-__token__}
token=${PYPI_TOKEN:-}
if [ -z "$token" ] && [ -n "${CI_JOB_TOKEN:-}" ]; then
  token=$CI_JOB_TOKEN
  user=gitlab-ci-token
fi
[ -n "$api" ] || { echo "ERROR: set GITLAB_API_URL (or run in CI)" >&2; exit 1; }
[ -n "$project" ] || { echo "ERROR: set PYPI_PROJECT (or run in CI)" >&2; exit 1; }
[ -n "$token" ] || { echo "ERROR: set PYPI_TOKEN (or run in CI)" >&2; exit 1; }
for tool in curl unzip sha256sum; do
  command -v "$tool" >/dev/null 2>&1 || { echo "ERROR: $tool is not installed" >&2; exit 1; }
done

# A project path goes in the URL encoded; a numeric id as it is.
case "$project" in
  *[!0-9]*) project=$(printf '%s' "$project" | sed 's|/|%2F|g') ;;
esac
base="${api%/}/projects/$project/packages/pypi"
wheelhouse=${WHEELHOUSE_DIR:-$(cd "$(dirname "$0")" && pwd)/wheelhouse}

# The token goes to curl on stdin as a config line, never on the command line.
curl_auth() {
  if [ -n "${CA_BUNDLE:-}" ]; then
    set -- --cacert "$CA_BUNDLE" "$@"
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
    *) echo "WARNING: ignoring wheelhouse/$file: only wheels are published" >&2; continue ;;
  esac
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
      *) echo "ERROR: GET $base/simple/$key: HTTP $code" >&2; exit 1 ;;
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
    20?) echo "uploaded $file"; uploaded=$((uploaded + 1)); echo "$file" >> "$index" ;;
    400|409)
      if grep -qi taken "$work/reply"; then
        skipped=$((skipped + 1))  # uploaded by a concurrent run
      else
        echo "ERROR: upload $file: HTTP $code: $(head -c 300 "$work/reply")" >&2; exit 1
      fi ;;
    *) echo "ERROR: upload $file: HTTP $code: $(head -c 300 "$work/reply")" >&2; exit 1 ;;
  esac
done

verb=uploaded
[ "$dry_run" = 1 ] && verb="to upload"
echo "$total file(s): $uploaded $verb, $skipped already in the registry"
