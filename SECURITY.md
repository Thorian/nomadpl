# Security

NomadPL is pre-1.0 software. It has tests for URL validation, static path isolation, response headers and gateway origins, but has not had an independent security audit.

The browser gateway binds to loopback. Keep each remote destination on its own hostname. Remote HTML and JavaScript execute in the browser, and application authentication remains the responsibility of the hosted application. Protect identity files with operating-system permissions.

For a suspected vulnerability, use the repository's private vulnerability reporting feature when available. Do not put private keys, real user data or exploit details into a public issue. For ordinary bugs, include a minimal reproduction using temporary identities.

Only the current development version receives fixes. Changes that affect protocol compatibility will be called out in the changelog.
