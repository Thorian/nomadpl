# NomadPL wire protocol v1

NomadPL uses a dedicated destination namespace: application name `nomadpl`, single aspect `node`. Incoming destinations are `SINGLE`. NomadNet continues to use `nomadnetwork.node`. The namespace participates in destination hashing, so even the same identity produces distinct addresses for the two protocols. Name announcement app data is the UTF-8 site name, scoped to `nomadpl.node`. Clients must filter announcements for `nomadpl.node`, not `nomadnetwork.node`.

Sources for the underlying API: [Reticulum request/link reference](https://reticulum.network/manual/reference.html) and [NomadNet node implementation](https://github.com/markqvist/NomadNet/blob/master/nomadnet/Node.py). Reticulum routing and encryption are unchanged; no transport or interface changes are required. Gateways that merely route Reticulum packets remain compatible. Gateways that translate/render NomadNet documents need to understand the following envelope for full web behavior.

## Request

Open a standard encrypted RNS Link to the destination and send `link.request("/nomadpl/http", data=envelope, ...)`. RNS's own MessagePack request encoding and Resource transfers carry the data; there is no additional JSON or base64 encoding.

```text
{
  "version": 1,
  "method": "POST",
  "target": "/submit?tag=a&tag=b",
  "headers": [["Content-Type", "application/x-www-form-urlencoded"]],
  "body": <raw bytes>
}
```

The target is ASCII origin-form, beginning with one `/`, with percent encoding where needed. Scheme, authority and fragment are not included. The query is not decoded/re-encoded by the transport. Header names are ASCII HTTP tokens and values use the HTTP Latin-1 range without CR/LF or other forbidden controls. Header lists preserve repeated fields; never collapse Set-Cookie into a dictionary. The body is binary. This version buffers one complete exchange.

## Response

```text
{
  "version": 1,
  "status": 200,
  "headers": [["Content-Type", "text/html; charset=utf-8"]],
  "body": <raw bytes>
}
```

Only final HTTP statuses 200–599 are returned; no informational response stream. HEAD, 204, 205 and 304 carry no body. At the HTTP gateway, hop-by-hop headers and any fields named by Connection are removed. Content-Length is recalculated for transmitted bodies, with HEAD/304 representation lengths preserved when supplied. The gateway maps peer/transport failures to 502 and timeouts to 504. It validates remote headers before sending them to the browser.

Applications use ordinary web paths. The destination registers only the `/nomadpl/http` exchange handler; it does not register NomadNet `/page/...` aliases or expose HTML on `nomadnetwork.node`. Existing Micron nodes and browsers therefore retain their own protocol and discovery namespace.

## Addressing and browser origins

Shareable addresses use `nomadpl://<32-hex NomadPL destination>/path?query#fragment`. The hash is computed from the identity and `nomadpl.node` namespace. Reticulum routing is unchanged. The CLI browser bridge maps one origin to `http://<destination>.localhost:<port>/`. It mounts the root directly, preserving ordinary relative links, root-relative assets, forms and redirect paths. Different destinations should always use different browser hostnames; different ports alone do not isolate cookies. Fragments remain client-side.

Clients verify that the recalled identity and `nomadpl.node` namespace reproduce the selected destination hash before opening a link. A NomadNet or other protocol address must be rejected, even if that identity also hosts a NomadPL site.

Public static sites permit anonymous incoming links, as ordinary public NomadNet sites do. A custom application can implement access control. A provided `Client(identity=...)` identifies its Reticulum link. Since 0.3.0, applications receive the verified identity hash in the local Request.remote_identity field; this field is never accepted from the wire. WSGI receives REMOTE_USER and AUTH_TYPE. Application authorization remains separate.

## Application admission (0.2.1)

Sites reject work beyond their concurrency or token-bucket admission limits with an ordinary 503 response. This does not change the wire version or destination namespace. Static hosts must keep private keys outside their public roots; the CLI enforces this and defaults to `public/`. Static serving denies directories named `state`.
