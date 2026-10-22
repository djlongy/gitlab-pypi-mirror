# SBOM and vulnerability scan

Run pipeline (or a schedule) with `MIRROR_SCAN=true` adds three jobs after `sync`:

| Job | Image | Does |
|---|---|---|
| `sync` | `PYTHON_IMAGE` | Unpacks each wheel's `.dist-info` from the wheelhouse into `sbom/wheels/<wheel>/`. Syft cannot read a `.whl` itself |
| `sbom` | `SYFT_IMAGE` | `syft scan dir:sbom/wheels`: one CycloneDX SBOM, `sbom/pypi-mirror.cdx.json`, with a `pkg:pypi` purl and licence per wheel |
| `scan` | `GRYPE_IMAGE` | `grype sbom:...`: a table in the log and `sbom/pypi-mirror.grype.json`. Fails on `GRYPE_FAIL_ON` (default `critical`) when a fixed version exists |
| `keep` | `PYTHON_IMAGE` | Uploads both files to the generic package `pypi-mirror-sbom/<YYYY-MM-DD>/`, also when `scan` failed |

Every run lists every wheel in the wheelhouse, so every run checks them all against the current database.

## What it does not do

- It is not a gate. The wheels are in the registry and asked of NiFi before `scan` runs. A failed `scan`
  names the package to update in `packages/<target>/requirements.txt`.
- The SBOM lists Python packages only. A library a wheel bundles (OpenSSL in `cryptography`) is not
  listed, but its advisories reach the wheel's own package as GHSAs.
- Sdists published with `MIRROR_PUBLISH_SDISTS` are not listed.

## Grype's database

Grype downloads its database from `grype.anchore.io` before the scan. Where the proxy blocks it, `scan`
stops with `no vulnerability database from ...`. Either allow `grype.anchore.io` through the proxy, or
host a copy and set `GRYPE_DB_UPDATE_URL` to its base (the URL that serves `v6/latest.json`). Grype refuses
a database older than 5 days (`GRYPE_DB_MAX_ALLOWED_BUILT_AGE`), so refresh a hosted copy at least that often.

## Run it by hand

```bash
mkdir -p sbom/wheels
for wheel in wheelhouse/*.whl; do unzip -q "$wheel" '*.dist-info/*' -d "sbom/wheels/$(basename "$wheel")"; done
syft scan dir:sbom/wheels -o cyclonedx-json=sbom/pypi-mirror.cdx.json
grype sbom:sbom/pypi-mirror.cdx.json --only-fixed
```
