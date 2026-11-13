# NomadPL

A separately distributable Python library for HTML, CSS, images and web applications over Reticulum, using dedicated **`nomadpl.node` destinations**.

Version 0.2.0 is an experimental, pre-1.0 release. APIs may change; see the changelog before upgrading.

For development, see [CONTRIBUTING.md](CONTRIBUTING.md). GitHub publication and distribution checks are in [RELEASING.md](RELEASING.md); version history is in [CHANGELOG.md](CHANGELOG.md).

NomadPL uses its own application name, `nomadpl`, with the `node` aspect. NomadNet remains on `nomadnetwork.node`. Even with the same identity key, these produce different destination addresses and independently scoped announcements. Existing Reticulum transports and TCP/radio gateways carry both without changes. A NomadPL-aware client or the included browser gateway handles HTTP methods, status codes, headers and binary bodies. NomadNet browsers do not discover these as NomadNet sites.

## Install and serve

Python 3.11+; the only runtime dependency is `rns>=1.6.0,<2`. NomadNet's UI and LXMF are not required.

```sh
python -m pip install .
nomadpl serve --root examples/site --identity state/home.identity --name "My site"
```

The command prints `nomadpl://<destination>/`. It uses your normal Reticulum instance and interfaces. To use another instance configuration, put `--rns-config /path/to/config-directory` before the subcommand. Enable a `TCPServerInterface` in that Reticulum configuration for inbound TCP, exactly as for a NomadNet server. This library does not rewrite your network configuration.

Multiple independent identities/sites in one process:

```sh
nomadpl serve --sites examples/sites.toml
```

Paths in that file are relative to the file itself. Each site needs a separate persistent identity file. Back up those files: the identity determines the site's destination address. Existing valid RNS identity files can be reused. Do not run two hosts with the same identity. Announcements begin after three seconds and repeat hourly; `--announce-interval` changes the interval, with a minimum of 60 seconds.

## Browse with an ordinary browser

On the browsing computer, with Reticulum connectivity to the server:

```sh
nomadpl browse nomadpl://DESTINATION_HASH/ --port 8080
```

Open the printed `http://<destination>.localhost:8080/` address. Current browsers resolve `.localhost` to loopback. If yours does not, map that exact hostname to `127.0.0.1` locally. The gateway binds only to loopback and accepts that exact hostname. Each destination gets its own browser hostname, keeping cookies and browser storage separate. For a second destination, start another gateway on another port.

The remote site is mounted at `/`, so `/assets/style.css`, `../images/photo.png`, CSS `url(...)`, redirects and form actions work without rewriting HTML. JavaScript can use same-origin `fetch()` as on a normal site. HTML, JavaScript and other assets execute in the browser, not on the Python host. Regular external HTTP/HTTPS links use the browser's ordinary Internet connection.

`nomadpl://` is a library/CLI address format; this package does not register an OS browser protocol handler. Links to another NomadPL destination require its gateway; `URL.resolve()` handles cross-destination NomadPL references for client integrations. Fragments such as `#section` stay in the browser and are never sent to the server.

## URL and HTTP features

| Feature | Behavior |
| --- | --- |
| Paths | Root-relative and document-relative URLs; browser dot-segment resolution |
| Encoding | Percent-encoded UTF-8 filenames, spaces and nested directories |
| Query strings | Preserved byte-for-byte, including repeated keys, empty values and encoded separators |
| Fragments | Native browser anchors; preserved by the URL helper and CLI |
| Directories | `index.html`; 308 trailing-slash redirects preserve query strings |
| Content | HTML, CSS, images, fonts, JavaScript, JSON and arbitrary binary downloads with MIME types |
| Static methods | GET, HEAD and OPTIONS; other methods return 405 |
| Dynamic methods | WSGI applications receive GET, POST, PUT, PATCH, DELETE and custom HTTP methods |
| Forms/uploads | Request bodies and content types preserved, including URL-encoded and multipart bodies |
| Redirects | HTTP status and Location passed through; same-origin absolute redirects use the gateway Host |
| Cookies | Repeated Set-Cookie headers and request Cookie headers preserved |
| Cache validation | Strong ETags, Last-Modified, If-None-Match, If-Modified-Since and 304 responses |
| Partial files | Single byte ranges, suffix ranges, If-Range, 206 and 416 |

Static hosts refuse path traversal, paths outside the site root, hidden files, Windows alternate-stream paths and `.allowed` sidecars/private pages. Directory listings and executable page scripts are disabled. Add/remove files without restarting. No NomadNet `/page/...` aliases are registered.

This is a bounded request/response web transport, not a full replacement for every HTTP server feature. Defaults: 2 MiB request bodies, 16 MiB response bodies, 8 KiB URLs, 64 KiB headers, 120-second total network request timeout, and 32 browser connections. Configure `Limits` in library integrations; `--timeout` changes the CLI network timeout. Files and WSGI responses are buffered. WebSockets, streaming/SSE, HTTP/2, CONNECT, TRACE, chunked uploads, multipart byte ranges and reverse-proxy TLS termination are not implemented. The loopback gateway denies requests initiated by other browser origins. For public Internet HTTP serving, put your application behind a maintained HTTP server rather than exposing this local bridge.

## Library use

```python
import RNS
from nomadpl import Site, StaticSite, Client, Request
from nomadpl.transport import load_identity

RNS.Reticulum()
site = Site(load_identity("state/site.identity"), StaticSite("public"), name="My site")
site.announce()
# Keep the host process alive; call announce periodically from your service.

# On another connected Reticulum instance:
client = Client("0123456789abcdef0123456789abcdef")
try:
    response = client.request(Request("GET", "/about/?lang=en"))
    print(response.status, response.headers, response.body)
finally:
    client.close()
```

A NomadPL site may coexist with a NomadNet site in the same process, even under the same identity, because their destination namespaces differ. `Site` always creates its own `nomadpl.node` destination and does not accept a pre-existing destination. Do not construct two NomadPL incoming destinations with the same identity.

`Gateway(("127.0.0.1", port), client, canonical_host=destination + ".localhost")` exposes a client to browsers. Give each remote destination a distinct hostname, as the CLI does; cookies are not isolated by port alone.

## Dynamic applications

Wrap a standard WSGI callable in `WSGIApplication` and pass it to `Site`. It receives the path, untouched query string, HTTP headers, method and a binary input stream; its status, repeated headers and body are returned to the browser. The adapter supplies the gateway's Host to the app for redirects. WSGI apps are responsible for authentication, cookie policy, routing and request-body parsing. Reticulum identity is not automatically an authenticated web login.

```sh
cd examples
nomadpl serve --app echo_app:application --identity state/app.identity --name "Echo app"
```

Because importing an installed command does not add the working directory to Python's module search path on every platform, install your application module or set `PYTHONPATH` to its directory when using `--app`. The example returns JSON for forms and has a `/redirect` route. Flask/Django and other WSGI frameworks can be used through their WSGI callables; they are optional application dependencies.

## Integrate with Reticulum Host

The sibling `server` project supports `format = "html"` in each site entry. Install this package into that server's environment and use `server/server-html.toml` to run its LXMF propagation, inbound TCP and multiple HTML sites together. The library itself remains independently packaged.

## Test and distribute

```sh
python -m unittest discover -s tests -v
# Enable the real two-process Reticulum/TCP integration test:
# PowerShell: $env:NOMADPL_NETWORK_TEST='1'
# POSIX: export NOMADPL_NETWORK_TEST=1
python -m unittest discover -s tests -v
python -m pip install build
python -m build
```

Distribute `dist/nomadpl-0.2.0-py3-none-any.whl` or the source archive. Installation does not depend on the sibling projects or the developer's source paths. Nothing is published to a package registry automatically. NomadPL code is MIT-licensed; the separately installed Reticulum dependency retains its own license. See [the wire protocol](docs/protocol.md) for gateway implementations.

Version 0.2.0 changes the destination namespace from the initial prototype. Reuse existing identity files, but update bookmarks and browser gateway targets to the newly printed addresses. The old 0.1.0 destination addresses are not NomadPL 0.2.0 addresses.
