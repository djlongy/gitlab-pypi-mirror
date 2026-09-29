# NiFi: publish each wheel straight to GitLab

`nifi/pypi-to-gitlab.json` is a NiFi 2.x flow that receives a bundle from
`mirror.py publish` or `export` (`NIFI_URL`), checks it, unpacks the wheels and
uploads each one to a GitLab PyPI registry. It uses native processors only: no
scripts. Tested on NiFi 2.11.0 against GitLab 18.9, then installed with pip.

## What mirror.py sends

One `POST` per bundle, the raw tar as the body. Header names are sent exactly as
written here.

| Header | Value |
|---|---|
| `Content-Type` | `application/x-tar` |
| `Filename` | `pypi-<kind>-<UTC yyyymmddTHHMMSS><ms>Z.tar`, e.g. `pypi-delta-20260929T012502376Z.tar` |
| `X-Sha256` | sha256 of the whole tar, 64 lowercase hex |
| `X-Artifact-Type` | `python-packages` |
| `X-Artifact-Format` | `tar` |
| `X-Artifact-Action` | `mirror` |
| `X-Bundle-Kind` | `delta` from `publish`, `since-YYYY-MM-DD` from `export` |

The `.sha256` sidecar stays on disk; `X-Sha256` carries the same value.

Tar layout (plain tar, not compressed):

```text
MANIFEST.json                                 created, kind, source, commit, files[{file, sha256, size}], requirements[]
wheels/<name>-<version>-<tags>.whl            one per wheel
requirements/<target>/requirements.txt        and platforms.txt, when BUNDLE_REQUIREMENTS is on
repo.bundle                                   only when BUNDLE_GIT is on
```

## What GitLab needs

`POST https://<gitlab>/api/v4/projects/<id>/packages/pypi`, `multipart/form-data`,
basic auth with any username and the token as the password.

| Form field | Needed | Source in the flow |
|---|---|---|
| `content` | yes, with the wheel's filename | the unpacked wheel |
| `name`, `version` | yes: without them GitLab returns 400 | the wheel filename |
| `sha256_digest` | yes in practice: GitLab accepts an upload without it, but the index then serves `#sha256=` empty and pip gets a 404; a wrong value makes pip fail its hash check. GitLab never computes or checks it | `CryptographicHashContent` on the wheel |
| `md5_digest` | no | not sent |
| `requires_python` | no: pip still refuses an incompatible wheel from its own metadata (`requires a different Python`) | not sent |

The token needs role Developer and scope `api`. Deleting a package needs Maintainer.
A second upload of the same file returns 400 with `has already been taken`.

## The flow

Top to bottom, in process group `pypi-to-gitlab`, parameter context `pypi-to-gitlab`:

| # | Processor | Type | Setting |
|---|---|---|---|
| 1 | Receive bundle | ListenHTTP | Listening Port `#{listen.port}`, Base Path `contentListener`, HTTP Headers for Attributes `(?i)x-.*` |
| 2 | Python packages only | RouteOnAttribute | `python-packages` = `${X-Artifact-Type:equals('python-packages'):and(${X-Artifact-Action:equals('mirror')}):and(${X-Artifact-Format:equals('tar')})}`; unmatched auto-terminated, or wire it to your other feeds |
| 3 | Hash bundle | CryptographicHashContent | SHA-256, writes `content_SHA-256` |
| 4 | Bundle matches X-Sha256 | RouteOnAttribute | `verified` = `${content_SHA-256:equals(${X-Sha256})}`; unmatched to Rejected |
| 5 | Unpack wheels | UnpackContent | Packaging Format `tar`, File Filter `wheels/.*\.whl`; original auto-terminated |
| 6 | Hash wheel | CryptographicHashContent | SHA-256, overwrites `content_SHA-256` with the wheel's hash |
| 7 | Name and version from filename | UpdateAttribute | `pypi.name` = `${filename:substringBefore('-')}`, `pypi.version` = `${filename:substringAfter('-'):substringBefore('-')}` |
| 8 | Upload to GitLab | InvokeHTTP | Method `POST`, URL `#{gitlab.pypi.url}`, Username `#{gitlab.username}`, Password `#{gitlab.token}`, Multipart Form-Data Name `content`, Filename Enabled `true`, `post:form:name` = `${pypi.name}`, `post:form:version` = `${pypi.version}`, `post:form:sha256_digest` = `${content_SHA-256}`, Response Body Attribute Name `gitlab.response`; Response auto-terminated, Retry loops to itself |
| 9 | Already in GitLab | RouteOnAttribute | `duplicate` = `${invokehttp.status.code:equals('400'):and(${gitlab.response:contains('taken')})}` |
| 10 | Uploaded | UpdateAttribute (end point) | receives Original and duplicate |
| 11 | Rejected (inspect queue) | UpdateAttribute, left stopped | receives every failure so it waits in the queue |

Parameters: `gitlab.pypi.url`, `gitlab.username` (`__token__`, any value works),
`gitlab.token` (**sensitive**), `listen.port`.

## Step by step

1. Create the token: in the GitLab project, **Settings > Access tokens**, role
   Developer, scope `api`. Note the project ID from **Settings > General**.
2. Import the flow: drag a Process Group onto the canvas, choose **Upload**, and pick
   `nifi/pypi-to-gitlab.json`.
3. Open the `pypi-to-gitlab` parameter context and set `gitlab.pypi.url` to
   `https://<gitlab>/api/v4/projects/<id>/packages/pypi` and `gitlab.token` to the
   token. `gitlab.token` is sensitive, so it is never exported with the flow.
4. If your GitLab uses an internal CA, add an SSL Context Service with that CA in its
   truststore and select it on **Upload to GitLab**.
5. Start every processor except **Uploaded** and **Rejected (inspect queue)**. Leaving
   those two stopped keeps their queues as a record; start **Uploaded** to drain it.
6. On the low side, point the mirror at the listener:
   `NIFI_URL=http://<nifi>:<listen.port>/contentListener`, then run `publish`, or run
   `export --bundle bundle` to resend history.
7. Check: every wheel reaches **Uploaded**, and
   `pip install --index-url https://__token__:<token>@<gitlab>/api/v4/projects/<id>/packages/pypi/simple <package>`
   works.

## What each queue means

| Queue | Meaning | Action |
|---|---|---|
| Upload to GitLab -> Uploaded (Original) | uploaded | none |
| Already in GitLab -> Uploaded (duplicate) | the file was already in the registry | none |
| Bundle matches X-Sha256 -> Rejected | the tar does not match its header: damaged or tampered | resend the bundle |
| Upload to GitLab -> Already in GitLab -> Rejected (unmatched) | GitLab refused it for another reason; `gitlab.response` and `invokehttp.status.code` say why (401 token, 403 role, 404 project) | fix, then empty or replay the queue |
| Hash or Unpack -> Rejected | unreadable content | inspect the flowfile |

## Tested

With `mirror.py`'s own `write_bundle` and `post_bundle` against NiFi 2.11.0:

- A 3-wheel bundle: 3 uploads, then `pip install -r requirements.txt` from the GitLab
  index installed all three, with the index hashes equal to the wheels' sha256.
- The same bundle again: 3 routed as duplicates, none rejected.
- A wrong `X-Sha256`: rejected before unpacking.
- `X-Artifact-Type: container-images`: ignored by this flow.
- A wheel with `Requires-Python: >=4.0`, uploaded without `requires_python`: pip refused
  it with `requires a different Python`.
