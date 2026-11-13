"""Loopback HTTP gateway: one Reticulum origin per browser port."""

import ipaddress
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from .http import Limits, Request, Response, end_to_end


class Gateway(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        address,
        client,
        *,
        limits=Limits(),
        max_connections=32,
        canonical_host=None,
    ):
        """Bind a loopback gateway and set its request and connection limits."""
        if not ipaddress.ip_address(address[0]).is_loopback:
            raise ValueError("Browser gateways must bind to a loopback IP")
        self.client, self.limits = client, limits
        self.canonical_host = canonical_host
        self._slots = threading.BoundedSemaphore(max_connections)
        super().__init__(address, Handler)

    def process_request(self, request, client_address):
        """Reserve a worker slot or close connections beyond the limit."""
        if not self._slots.acquire(blocking=False):
            request.close()
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        """Handle a connection and release its slot even after an error."""
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "NomadPL/0.2"

    def setup(self):
        """Initialize the browser connection and apply the read timeout."""
        super().setup()
        self.connection.settimeout(self.server.limits.timeout)

    def log_message(self, fmt, *args):
        """Send HTTP access messages through the application's logger."""
        logging.info("Browser: " + fmt, *args)

    def _valid_host(self):
        """Accept only the configured local hostname and listening port."""
        hosts = self.headers.get_all("Host", [])
        if len(hosts) != 1:
            return False
        try:
            parsed = urlsplit("http://" + hosts[0])
            expected_port = self.server.server_address[1]
            allowed = (
                (self.server.canonical_host,)
                if self.server.canonical_host
                else ("localhost", "127.0.0.1", "::1")
            )
            return (
                parsed.hostname in allowed
                and parsed.port == expected_port
                and not parsed.username
                and not parsed.password
                and not parsed.path
                and not parsed.query
                and not parsed.fragment
            )
        except ValueError:
            return False

    def _send(self, response):
        """Write a validated response, fixing framing and suppressing HEAD bodies."""
        self.send_response(response.status)
        headers = end_to_end(response.headers)
        has_length = False
        for key, value in headers:
            if key.lower() == "content-length":
                if self.command == "HEAD" or response.status == 304:
                    if value.isdigit():
                        self.send_header(key, value)
                        has_length = True
                continue
            self.send_header(key, value)
        if not has_length and response.status not in (204, 304):
            self.send_header("Content-Length", str(len(response.body)))
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD" and response.status not in (204, 205, 304):
            self.wfile.write(response.body)

    def _handle(self):
        # Reject requests made through DNS rebinding or a different web origin.
        # Remote site scripts still execute normally inside this dedicated origin.
        """Validate a browser request before forwarding it over Reticulum."""
        if not self._valid_host():
            self.close_connection = True
            return self._send(Response.text(421, "Invalid gateway host"))
        origin = self.headers.get("Origin")
        if origin and origin != "http://" + self.headers["Host"]:
            self.close_connection = True
            return self._send(Response.text(403, "Cross-origin gateway access denied"))
        if self.command in ("CONNECT", "TRACE"):
            self.close_connection = True
            return self._send(Response.text(405, "Method not supported"))
        if self.headers.get("Upgrade") or self.headers.get("Transfer-Encoding"):
            self.close_connection = True
            return self._send(
                Response.text(501, "Upgrades and chunked uploads are not supported")
            )
        lengths = self.headers.get_all("Content-Length", [])
        try:
            if len(lengths) > 1 or (
                lengths and (not lengths[0].isascii() or not lengths[0].isdigit())
            ):
                raise ValueError("Invalid content length")
            length = int(lengths[0]) if lengths else 0
            if length > self.server.limits.request_body:
                self.close_connection = True
                return self._send(Response.text(413, "Request body too large"))
            body = self.rfile.read(length)
            if len(body) != length:
                raise ValueError("Incomplete request body")
            headers = end_to_end(list(self.headers.items()))
            response = self.server.client.request(
                Request(self.command, self.path, headers, body).validate(
                    self.server.limits
                )
            )
            response.validate(self.server.limits)
        except (ValueError, TypeError):
            self.close_connection = True
            return self._send(Response.text(400, "Invalid request"))
        except TimeoutError:
            self.close_connection = True
            return self._send(Response.text(504, "Reticulum request timed out"))
        except (ConnectionError, OSError):
            self.close_connection = True
            return self._send(Response.text(502, "Reticulum peer unavailable"))
        self._send(response)

    def __getattr__(self, name):
        """Route HTTP method handlers through the common validation boundary."""
        if name.startswith("do_"):
            return self._handle
        raise AttributeError(name)
