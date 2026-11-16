# Headless page hosting

The host serves NomadNet Micron pages and NomadPL web pages without the NomadNet
terminal application. It runs as one foreground process for a service manager to
start and stop. PHP and other interpreters are installed separately.

## Try the example

Install a supported, fully patched PHP release with its CGI executable. On Debian
or Ubuntu, from the repository root:

```sh
sudo apt-get install php-cgi
python -m pip install .
nomadpl host --config examples/host/host.toml --check
nomadpl host --config examples/host/host.toml
```

On Windows, change the example interpreter command to an absolute path:

```toml
[interpreters.php]
command = ["C:/PHP/php-cgi.exe", "-d", "display_errors=0", "-d", "log_errors=1", "-d", "cgi.fix_pathinfo=0"]
suffixes = [".php"]
output = "cgi"
```

Use php-cgi, not php.exe. PHP's CLI executable does not populate web request
variables in the same way. The host supplies CGI variables and REDIRECT_STATUS;
there is no need to disable PHP's CGI redirect protection. Query strings never
become interpreter command-line arguments.

The host uses your existing Reticulum instance by default. For a dedicated
instance with inbound Reticulum TCP on port 4242:

```sh
nomadpl --rns-config examples/host/reticulum host --config examples/host/host.toml
```

The example listener binds to all IPv4 interfaces. Allow TCP 4242 through the
firewall only when outside connections are wanted. This is Reticulum TCP, not
Internet HTTP. Existing Reticulum configuration is not rewritten.

Startup prints both addresses. Open the NomadNet address in NomadNet. For NomadPL:

```sh
nomadpl browse nomadpl://DESTINATION/ --port 8080
```

Visit the printed browser address. Both example sites have a PHP form. NomadNet
pages still output Micron; PHP does not convert HTML to Micron.

## Sites and state

Copy the example directory to your service directory before editing it. Paths in
the TOML file are relative to that file. Add another [[sites]] entry with a unique
id and separate roots to host another site. A site may enable NomadPL, NomadNet
or both.

One key per site is saved under the private state directory. The same key produces
different addresses for the two protocol namespaces. Keep the site id and key to
retain addresses across restarts. An optional site identity setting, such as
`identity = "state/existing.identity"`, selects an existing key inside state.
Duplicate keys are rejected. A process lock prevents two hosts from sharing state.

Public roots must be separate from state and must not contain host configuration.
Hidden paths, state directories, traversal and escaping symlinks are refused.
Known script suffixes are not served as source without an interpreter. Put only
public assets and intended page scripts in these roots; keep includes,
dependencies, backups and private application data elsewhere.

File contents are read on every request. NomadNet page paths are registered at
startup; restart after adding or removing files or changing configuration.
Announcements begin shortly after startup and repeat at announce_interval seconds.

## External interpreters

Each interpreter has an argument list, suffixes and output format. Each site's
protocol settings select which named interpreters it enables. For example:

```toml
[interpreters.python]
command = ["/usr/bin/python3", "{script}"]
suffixes = [".cgi"]
output = "cgi"
```

The host replaces {script} with the resolved filename. It does not invoke a shell,
infer commands from executable bits or execute an upload merely because it is
executable. Keep uploads outside roots with enabled script suffixes.

CGI programs write headers, a blank line, then body bytes to stdout. They may set
Status, Location, repeated Set-Cookie fields and binary content. Location defaults
to status 302 when no explicit status is given. Hop-by-hop headers are removed.
Use output = "body" for programs emitting only text; these return text/plain.
Use CGI headers for HTML or other content types.

script_timeout defaults to 30 seconds. The normal request and response limits
apply; stderr is capped at 64 KiB. Both output pipes are drained and interpreters
are stopped on timeout or excessive output. Error responses do not include stderr
or local paths. The PHP example also disables display_errors; keep it disabled on public sites.

Scripts run as the service's OS user. This is not a sandbox for untrusted site
authors: scripts can access that user's files and network and start other
processes. Use separate service accounts or containers for different owners.
Process cleanup is best effort on Windows; use OS resource limits to constrain
arbitrary descendants, CPU and memory.

## Forms

NomadPL passes the method, query, content type and raw body through CGI. PHP handles
GET, POST, cookies and uploaded files normally, subject to PHP's configuration.
URL-encoded forms, arrays and multipart uploads are supported.

NomadNet's var_view becomes `$_GET['view']`; field_name becomes
`$_POST['name']`. Requests with fields use POST; other requests use GET. Original
field_* and var_* values are also available through getenv() for existing page
scripts. NomadNet fields have no general binary-upload representation.

Applications still need input validation, output escaping, CSRF protection and
their own authorization rules.

## Visitor identification

Identity comes only from the verified Reticulum link. Headers, cookies, form
fields and wire-envelope values cannot select an authenticated identity.
The value is the primary identity hash, not an LXMF or page destination.

| PHP value | Identified request |
| --- | --- |
| `$_SERVER['REMOTE_USER']` | 32-character identity hash |
| `$_SERVER['AUTH_TYPE']` | Reticulum |
| `getenv('RNS_REMOTE_IDENTITY')` | Same identity hash |
| `getenv('remote_identity')` | Same value for NomadNet script compatibility |

Anonymous requests have empty values. HTTP_REMOTE_USER remains an untrusted
header, separate from REMOTE_USER. WSGI applications receive REMOTE_USER,
AUTH_TYPE and nomadpl.remote_identity (raw hash bytes, or None).

NomadNet uses its normal identify controls. With the NomadPL browser gateway:

```sh
nomadpl browse nomadpl://DESTINATION/ --identity /private/path/client.identity
```

The gateway identifies to that destination using the selected existing key.
The key stays on the client. This identity applies to every request through that
gateway, so use it only on a trusted machine and account. Browsing is anonymous
unless an identity is selected.

Set require_identity = true in a protocol's site settings to deny anonymous
requests. For a page allowlist, create hello.php.allowed with one allowed primary
identity hash per line. Empty, invalid, unreadable or escaping allowlists deny
access. Allowlists are read as data, never executed or made downloadable.

Possession of a verified key does not automatically make a user an administrator.
Pages must enforce permissions for their own actions.

## Running as a service

An example systemd unit is in examples/host/nomadpl-host.service. Create the
service account, install under /srv/nomadpl, copy the example files there and
adjust absolute paths before enabling it. Keep PHP and Reticulum updated.
On Windows, use a service manager with the same foreground command.

SIGINT and SIGTERM stop the announce loop. Allow in-flight interpreters to finish
before forcibly stopping the process. Back up private state while stopped.
