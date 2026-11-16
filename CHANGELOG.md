# Changelog

## 0.3.0

- Add a headless multi-site host for NomadNet and NomadPL.
- Run PHP and other configured page interpreters with CGI request data.
- Pass verified Reticulum identities to pages and WSGI applications.
- Support static identity allowlists through .allowed sidecars.
- Add browser identification with an explicitly selected identity file.
- Test real PHP forms, uploads and identified requests over both page protocols.

## 0.2.1

Keep identity files outside public roots; default to a dedicated public folder and block state paths. Bound incoming application work with concurrency and rate limits.

## 0.2.0

- Reject destination hashes from other protocols before opening a client link.
- Abort WSGI error replacement after response output starts, preserving clean error responses.
- Close browser connections after upload or transport timeouts instead of reusing damaged sockets.
- Use the dedicated `nomadpl.node` destination namespace.
- Serve HTML, stylesheets, images and other static assets over Reticulum links.
- Support query strings, redirects, conditional requests and single byte ranges.
- Add a WSGI adapter and a loopback browser gateway with separate site hostnames.
- Host multiple site identities and document the v1 wire envelope.
- Include formatting checks, real-network tests and standalone release instructions.

## 0.1.0 — local prototype

The original prototype shared NomadNet's destination namespace. It was replaced by the dedicated namespace before publication. Its destination addresses are not compatible with 0.2.0.
