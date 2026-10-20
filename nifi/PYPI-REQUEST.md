# NiFi: carry each wheel on request

`mirror.py request` POSTs a small JSON list of wheels. The low NiFi downloads each one
from the low GitLab registry and sends it across the link as its own FlowFile; the high
NiFi checks it and uploads it to the high GitLab registry. No POST carries a wheel, so a
wheel of any size crosses, and a link that splits large files handles each wheel on its
own. Native processors only. Tested on NiFi 2.12.0 against GitLab 18.

## What mirror.py sends

One `POST` to `MIRROR_REQUEST_URL`, body JSON:

```json
{"registry": "https://<gitlab>/api/v4/projects/<id>/packages/pypi",
 "files": [{"filename": "pyspark-4.0.1-py2.py3-none-any.whl", "name": "pyspark", "version": "4.0.1",
            "sha256": "<64 hex>", "size": 434813858,
            "url": "https://<gitlab>/api/v4/projects/<id>/packages/pypi/files/<sha256>/<filename>"}]}
```

Headers, names as written: `Content-Type: application/json`, `Filename` (`pypi-request-<UTC>.json`),
`X-Sha256` (of the body), `X-Artifact-Type: python-packages-request`, `X-Artifact-Format: json`,
`X-Artifact-Action: mirror`, `X-File-Count`, `X-Registry` (the body's registry), `X-CI-Project`,
`X-CI-Pipeline-Url` and `X-CI-Commit-Sha` in a pipeline (the same CI attributes as gitlab-registry-mirror's
request, so one route or audit serves both), and `Authorization: Bearer` when `MIRROR_REQUEST_TOKEN` is set
(https only).

## Two ways to build the flows

- **Python processors** (recommended): PlanPythonTransfer and RoutePythonTransfer from
  nifi-python-package-processors (https://github.com/djlongy/nifi-python-package-processors) do the
  checks this page builds from native processors, in 7 processors on the low side and 4 on the high
  side. They read only the JSON and attributes; native
  InvokeHTTP still moves every wheel. Their README and `docs/runbooks/pypi-transfer-build-by-hand.md`
  give the flow, properties and TLS setup.
- **Native processors only**: `flow.py --side request-low|request-high`, described below.

Both take the same request and route on the same attributes. The high side receives each wheel with
`X-Artifact-Type: python-packages`, so a RouteOnAttribute in front of either flow can split it off.

## The ledger

`request` names each wheel in the registry once, in POSTs of `MIRROR_REQUEST_BATCH` (200)
wheels. After each POST succeeds it records `sent: {filename: {sha256, time, pipeline}}` in the
ledger: in CI the generic package `pypi-mirror-ledger/v1/ledger.json` of the mirror project (the
job token reads and writes it), elsewhere `MIRROR_LEDGER` or `.mirror-ledger.json`. A failed
POST stops the run: the batches before it stay recorded, and the next run asks for the rest.

The ledger records what was sent, not what arrived: a one-way link sends no receipt.
The high side is idempotent (a wheel GitLab already holds routes to Uploaded), so to fill
a gap, request again: `request NAME[==VERSION]`, `request --all`, or in a pipeline
`MIRROR_RESEND_PACKAGES` and `MIRROR_RESEND_ALL=true`.

## The flows

Build them with `nifi/flow.py`, or import `nifi/pypi-request-low.json` and
`nifi/pypi-request-high.json` as process groups.

`pypi request-low`, parameter context `pypi request-low`:

| # | Processor | Type | Setting |
|---|---|---|---|
| 1 | Receive request | ListenHTTP | Port `#{request.port}`, Base Path `#{request.path}`, HTTP Headers for Attributes `(?i)x-.*` |
| 2 | Python package requests only | RouteOnAttribute | `X-Artifact-Type` is `python-packages-request` and `X-Artifact-Format` is `json` |
| 3, 4 | Hash request, Request matches X-Sha256 | CryptographicHashContent, RouteOnAttribute | the body against `X-Sha256` |
| 5 | One FlowFile per wheel | SplitJson | `$.files.*` |
| 6 | Wheel fields | EvaluateJsonPath | `pypi.filename`, `pypi.name`, `pypi.version`, `pypi.sha256`, `pypi.size`, `pypi.url` |
| 7 | Low registry base | UpdateAttribute | `pypi.allowed.base` = `#{gitlab.low.pypi.url}/files/` |
| 8 | From the low registry only | RouteOnAttribute | `pypi.url` starts with `pypi.allowed.base`, a plain wheel file name, a 64-hex sha256 |
| 9 | Fetch wheel | InvokeHTTP | `GET ${pypi.url}`, user `#{gitlab.username}`, password `#{gitlab.low.token}`; Retry loops |
| 10, 11 | Hash wheel, Wheel matches its sha256 | CryptographicHashContent, RouteOnAttribute | the download against `pypi.sha256` |
| 12 | Label wheel | UpdateAttribute | `filename`, `X-Sha256`, `X-Artifact-Type: python-packages`, `X-Artifact-Format: whl` |
| 13 | Keep attributes across the link | PackageFlowFile | every attribute rides inside the content |
| 14 | to diode | output port | |

The url comes from the request, so step 8 is what stops a request from making the low
NiFi fetch anything else. NiFi does not put a parameter into a quoted literal of an
expression, so step 7 brings the base in as an attribute after the request's own fields.

`pypi request-high`, parameter context `pypi request-high`:

| # | Processor | Type | Setting |
|---|---|---|---|
| 1 | from diode | input port | |
| 2 | Restore attributes | UnpackContent | Packaging Format `flowfile-stream-v3` |
| 3 | Wheels only | RouteOnAttribute | `python-packages`, `whl`, a plain wheel file name |
| 4, 5 | Hash wheel, Wheel matches X-Sha256 | CryptographicHashContent, RouteOnAttribute | |
| 6 | Upload to GitLab | InvokeHTTP | `POST #{gitlab.high.pypi.url}` multipart, form `name`, `version`, `sha256_digest`; Retry loops |
| 7 | Already in GitLab | RouteOnAttribute | HTTP 400 with `taken` |
| 8 | Uploaded | UpdateAttribute (end point) | Original and duplicate |

Each side has a stopped LogMessage **Rejected (inspect queue)**: every failure waits there.
Start it to let the queue go: it logs each FlowFile at warn as it passes.

Parameters: `request.port` (9310), `request.path` (`pypi-request`), `gitlab.username`
(`__token__`), `gitlab.low.pypi.url`, `gitlab.high.pypi.url`, and the sensitive
`gitlab.low.token` (reads packages) and `gitlab.high.token` (Developer, `api`); the test used Developer `api` tokens for both.

## Step by step

1. Build both sides. Tokens come from the environment and become sensitive parameters:

   ```sh
   GITLAB_LOW_TOKEN=... python3 nifi/flow.py --side request-low --url https://nifi-low:8443 --password-stdin \
     --param gitlab.low.pypi.url=https://gitlab-low/api/v4/projects/<id>/packages/pypi --connect-diode diode-low
   GITLAB_HIGH_TOKEN=... python3 nifi/flow.py --side request-high --url https://nifi-high:8443 --password-stdin \
     --param gitlab.high.pypi.url=https://gitlab-high/api/v4/projects/<id>/packages/pypi --connect-diode diode-high
   ```

   `--connect-diode GROUP` connects `to diode` to the input port `pypi` of that root group
   (low), or its output port `pypi` to `from diode` (high), and starts that port. Without it,
   connect the ports to your link yourself. With a client certificate instead of a password:
   `--client-cert tls.crt --client-key tls.key --ca-file ca.crt`.
2. In the mirror project set `MIRROR_REQUEST_URL` (and `MIRROR_REQUEST_TOKEN` if a proxy in
   front of the listener checks one). The next `sync` requests every wheel the registry holds.
3. Check: every wheel reaches **Uploaded** on the high side, and `pip index versions <name>`
   against the high index lists it.

## What each queue means

| Queue | Meaning | Action |
|---|---|---|
| Request matches X-Sha256 -> Rejected | the request body does not match its header | send it again |
| From the low registry only -> Rejected | a url outside the low registry, a file name with a path, or a bad sha256 | find who sent it |
| Fetch wheel -> Rejected | GitLab answered 4xx: 401 token, 404 no such file (wrong sha256 in the url) | fix, then replay |
| Wheel matches its sha256 -> Rejected | GitLab served other bytes than the request named | inspect |
| high: Wheel matches X-Sha256 -> Rejected | the wheel changed on the way | request it again |
| high: Already in GitLab -> Rejected | GitLab refused it for another reason; `gitlab.response` says why | fix, then replay |

## Tested

On NiFi 2.12.0, through an emulated diode that splits each file into chunks, encrypts them,
sends them in random order at 10 MB/s and reassembles them, and only lets `filename`,
`fragment.*`, `diode.*` and four Stroom headers cross:

- One request for 8 wheels, from 65 KB to 435 MB (`pyspark` built from its sdist, and
  `nvidia-nccl-cu12` at 323 MB): all 8 in the high registry within two minutes, each
  sha256 equal to the source, and pip downloaded them from the high index.
- `request` again: `nothing new to request`. `request --all`: 8 duplicates routed to Uploaded.
- A request with a foreign url, a `../` file name and a wrong sha256: all three rejected on
  the low side; a body that does not match `X-Sha256`: rejected before splitting.
- A POST to a closed port: `request` failed, the ledger kept no record, the next run asked again.
