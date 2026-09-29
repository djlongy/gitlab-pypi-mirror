# Build the pypi-to-gitlab flow by hand

The same flow as `nifi/pypi-to-gitlab.json`, for a NiFi where you cannot import a
file. Every value below comes from that JSON. Anything not listed stays at its
default. Written for the NiFi 2.x UI; `nifi/PYPI-TO-GITLAB.md` explains what the flow
does and why.

You build it in four parts: parameters, process group, 11 processors, 17 connections.
Property names, parameter names and relationship names are case-sensitive: type them
exactly as written.

## Before you start

- A GitLab project for the packages, its numeric project ID (**Settings > General**),
  and an access token with role **Developer** and scope **api**.
- A free port on the NiFi host for the listener, for example `9090`, open from where
  `mirror.py` runs.

## Part 1: parameter context

1. Top-right menu (**☰**) > **Parameter Contexts** > **+**.
2. **Settings** tab: Name `pypi-to-gitlab`.
3. **Parameters** tab, add four parameters with **+**. Choose **Sensitive: Yes** for
   `gitlab.token` when you create it: it cannot be changed afterwards.

   | Name | Value | Sensitive |
   |---|---|---|
   | `gitlab.pypi.url` | `https://<gitlab>/api/v4/projects/<id>/packages/pypi` | No |
   | `gitlab.username` | `__token__` | No |
   | `gitlab.token` | the access token | **Yes** |
   | `listen.port` | `9090` | No |

4. **Apply**.

## Part 2: process group

1. Drag the **Process Group** icon from the top toolbar onto the canvas. Name it
   `pypi-to-gitlab` and click **Add**.
2. Right-click it > **Configure** > **Settings** tab: set **Parameter Context** to
   `pypi-to-gitlab` and click **Apply**.
3. Double-click the group to go inside it. Everything below is built in here.

## Part 3: processors

For each one:
1. Drag the **Processor** icon onto the canvas, type the type into the filter, select
   it and click **Add**.
2. Right-click the processor > **Configure**.
3. **Settings** tab: set the Name.
4. **Properties** tab: set the listed properties. For a row marked *(add)*, click **+**
   at the top right of the properties table, enter the name, click **OK**, then enter
   the value.
5. **Relationships** tab: tick **terminate** for each relationship listed under
   Auto-terminate. Leave the others unticked, because the connections in Part 4 use
   them.
6. **Apply**.

Place them top to bottom in this order. Positions don't affect behaviour.

### 1. Receive bundle

Type **ListenHTTP**.

| Property | Value |
|---|---|
| Listening Port | `#{listen.port}` |
| Base Path | `contentListener` |
| HTTP Headers for Attributes | `(?i)x-.*` |

Auto-terminate: none.

### 2. Python packages only

Type **RouteOnAttribute**.

| Property | Value |
|---|---|
| Routing Strategy | `Route to Property name` (default) |
| `python-packages` *(add)* | `${X-Artifact-Type:equals('python-packages'):and(${X-Artifact-Action:equals('mirror')}):and(${X-Artifact-Format:equals('tar')})}` |

Auto-terminate: `unmatched`. If this listener also takes other feeds, connect
`unmatched` to them instead.

### 3. Hash bundle

Type **CryptographicHashContent**.

| Property | Value |
|---|---|
| Hash Algorithm | `SHA-256` (default) |

Auto-terminate: none.

### 4. Bundle matches X-Sha256

Type **RouteOnAttribute**.

| Property | Value |
|---|---|
| `verified` *(add)* | `${content_SHA-256:equals(${X-Sha256})}` |

Auto-terminate: none.

### 5. Unpack wheels

Type **UnpackContent**.

| Property | Value |
|---|---|
| Packaging Format | `tar` |
| File Filter | `wheels/.*\.whl` |

Auto-terminate: `original`.

### 6. Hash wheel

Type **CryptographicHashContent**.

| Property | Value |
|---|---|
| Hash Algorithm | `SHA-256` (default) |

Auto-terminate: none.

### 7. Name and version from filename

Type **UpdateAttribute**.

| Property | Value |
|---|---|
| `pypi.name` *(add)* | `${filename:substringBefore('-')}` |
| `pypi.version` *(add)* | `${filename:substringAfter('-'):substringBefore('-')}` |

Auto-terminate: none.

### 8. Upload to GitLab

Type **InvokeHTTP**.

| Property | Value |
|---|---|
| HTTP Method | `POST` |
| HTTP URL | `#{gitlab.pypi.url}` |
| Request Username | `#{gitlab.username}` |
| Request Password | `#{gitlab.token}` |
| Request Multipart Form-Data Name | `content` |
| Request Multipart Form-Data Filename Enabled | `true` (default) |
| Response Body Attribute Name | `gitlab.response` |
| `post:form:name` *(add)* | `${pypi.name}` |
| `post:form:version` *(add)* | `${pypi.version}` |
| `post:form:sha256_digest` *(add)* | `${content_SHA-256}` |

Auto-terminate: `Response`.

If GitLab uses an internal CA, also set **SSL Context Service**. Create a
StandardSSLContextService whose truststore holds that CA, then enable it.

For wheels of hundreds of MB, raise **Socket Write Timeout** and **Socket Read
Timeout** from the default `15 secs`, for example to `5 mins`. The tested flow used
the defaults with small wheels.

### 9. Already in GitLab

Type **RouteOnAttribute**.

| Property | Value |
|---|---|
| `duplicate` *(add)* | `${invokehttp.status.code:equals('400'):and(${gitlab.response:contains('taken')})}` |

Auto-terminate: none.

### 10. Uploaded

Type **UpdateAttribute**, no properties.

Auto-terminate: `success`. It is the end point for everything that reached GitLab.

### 11. Rejected (inspect queue)

Type **UpdateAttribute**, no properties.

Auto-terminate: `success`. Keep it **stopped**, so anything that failed waits in its
input queue for you to inspect.

## Part 4: connections

For each row: hover over the source processor, drag the arrow that appears onto the
destination, tick exactly the listed relationship(s) on the **Details** tab, and click
**Add**. For row 10, drag the arrow away and back onto the same processor.

| # | From | Relationship | To |
|---|---|---|---|
| 1 | Receive bundle | `success` | Python packages only |
| 2 | Python packages only | `python-packages` | Hash bundle |
| 3 | Hash bundle | `success` | Bundle matches X-Sha256 |
| 4 | Hash bundle | `failure` | Rejected (inspect queue) |
| 5 | Bundle matches X-Sha256 | `verified` | Unpack wheels |
| 6 | Bundle matches X-Sha256 | `unmatched` | Rejected (inspect queue) |
| 7 | Unpack wheels | `success` | Hash wheel |
| 8 | Unpack wheels | `failure` | Rejected (inspect queue) |
| 9 | Hash wheel | `success` | Name and version from filename |
| 10 | Upload to GitLab | `Retry` | Upload to GitLab (itself) |
| 11 | Hash wheel | `failure` | Rejected (inspect queue) |
| 12 | Name and version from filename | `success` | Upload to GitLab |
| 13 | Upload to GitLab | `Original` | Uploaded |
| 14 | Upload to GitLab | `No Retry` | Already in GitLab |
| 15 | Upload to GitLab | `Failure` | Rejected (inspect queue) |
| 16 | Already in GitLab | `duplicate` | Uploaded |
| 17 | Already in GitLab | `unmatched` | Rejected (inspect queue) |

## Part 5: check and start

1. Every processor except the two end points shows a stopped square, not a yellow
   warning triangle. Hover a triangle to read what is missing: usually an
   unterminated relationship or a mistyped property name.
2. Open **Upload to GitLab** > **Properties** and confirm that the only rows with a
   **delete** icon are the three `post:form:` ones. A mistyped built-in property also
   shows up as a deletable row, even though the processor still reports valid.
3. Select everything except **Rejected (inspect queue)**, right-click > **Start**.
4. From the low side, send a bundle:
   `NIFI_URL=http://<nifi>:9090/contentListener python3 mirror.py publish --bundle bundle`
5. Watch the queues: wheels arrive in front of **Uploaded**; nothing should reach
   **Rejected (inspect queue)**.
6. Install from GitLab to prove it:
   `pip install --index-url https://__token__:<token>@<gitlab>/api/v4/projects/<id>/packages/pypi/simple <package>`

If anything reaches **Rejected (inspect queue)**, right-click that queue > **List
queue** and open the flowfile's attributes:
- `invokehttp.status.code` and `gitlab.response` explain a GitLab refusal: 401 means
  the token, 403 the role, 404 the project ID in `gitlab.pypi.url`.
- A bundle rejected at **Bundle matches X-Sha256** was damaged in transit. Send it
  again.
