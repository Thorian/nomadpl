"""Release regressions for destination binding, WSGI failures and HTTP timeouts."""

import sys
import threading
import unittest
from http.client import HTTPConnection
from unittest.mock import patch

import RNS

from nomadpl import Limits, Request, Response, WSGIApplication
from nomadpl.admission import RequestGate
from nomadpl.gateway import Gateway
from nomadpl.transport import Client, Site


class DestinationTests(unittest.TestCase):
    def test_path_discovery_retries_when_connection_arrives_later(self):
        client = Client("ab" * 16, limits=Limits(timeout=30))
        with (
            patch.object(RNS.Transport, "has_path", side_effect=[False, False, True]),
            patch.object(RNS.Transport, "request_path") as request_path,
            patch.object(RNS.Identity, "recall", return_value=None),
            patch("nomadpl.transport.time.monotonic", side_effect=[0, 0, 10]),
            patch("nomadpl.transport.time.sleep"),
        ):
            with self.assertRaisesRegex(ConnectionError, "identity unavailable"):
                client.request(Request("GET", "/"))
        self.assertEqual(request_path.call_count, 2)

    def test_wrong_namespace_rejected_before_link_creation(self):
        """A valid identity cannot silently substitute its NomadPL address for another aspect."""
        identity = RNS.Identity()
        wrong = RNS.Destination.hash_from_name_and_identity(
            "nomadnetwork.node", identity
        )
        correct = RNS.Destination.hash_from_name_and_identity("nomadpl.node", identity)
        self.assertNotEqual(wrong, correct)
        client = Client(wrong.hex(), limits=Limits(timeout=0.2))
        with (
            patch.object(RNS.Transport, "has_path", return_value=True),
            patch.object(RNS.Identity, "recall", return_value=identity),
            patch.object(RNS, "Link") as link,
        ):
            for attempt in range(2):
                with self.assertRaisesRegex(ConnectionError, "not a nomadpl.node"):
                    client.request(Request("GET", "/"))
            link.assert_not_called()
        self.assertIsNone(client._link)


class WSGIErrorTests(unittest.TestCase):
    def test_error_replaces_headers_before_first_body(self):
        """An application may replace its uncommitted response using exc_info."""

        def application(environ, start_response):
            """Replace pending success headers before any body has been written."""
            start_response("200 OK", [("X-Original", "discard")])
            try:
                raise ValueError("render failed")
            except ValueError:
                start_response(
                    "500 Error", [("Content-Type", "text/plain")], sys.exc_info()
                )
            return [b"generic error"]

        response = WSGIApplication(application)(Request("GET", "/"))
        self.assertEqual((response.status, response.body), (500, b"generic error"))
        self.assertNotIn("X-Original", dict(response.headers))

    def test_error_after_write_aborts_without_returning_partial_bytes(self):
        """Written response bytes cannot be mixed into an error replacement."""

        def application(environ, start_response):
            """Attempt error replacement after committing a partial response."""
            write = start_response("200 OK", [("Content-Type", "text/plain")])
            write(b"private partial output")
            try:
                raise ValueError("render failed")
            except ValueError:
                start_response(
                    "500 Error", [("Content-Type", "text/plain")], sys.exc_info()
                )
            return [b"generic error"]

        app = WSGIApplication(application)
        with self.assertRaisesRegex(ValueError, "render failed"):
            app(Request("GET", "/"))
        site = Site.__new__(Site)
        site._gate = RequestGate()
        site.app, site.limits = app, Limits()
        with self.assertLogs(level="ERROR"):
            response = site._respond(
                "/nomadpl/http", Request("GET", "/").to_wire(), None, None, None, None
            )
        self.assertEqual(response["status"], 500)
        self.assertNotIn(b"private partial output", response["body"])

    def test_empty_yield_does_not_commit_response(self):
        """An empty iterator chunk leaves the application free to replace headers."""

        def application(environ, start_response):
            """Yield a heartbeat before choosing an error response."""
            start_response("200 OK", [("Content-Type", "text/plain")])
            yield b""
            try:
                raise ValueError("render failed")
            except ValueError:
                start_response(
                    "500 Error", [("Content-Type", "text/plain")], sys.exc_info()
                )
            yield b"generic error"

        response = WSGIApplication(application)(Request("GET", "/"))
        self.assertEqual((response.status, response.body), (500, b"generic error"))

    def test_explicit_empty_write_commits_response(self):
        """The WSGI write callable commits headers even when invoked with empty bytes."""

        def application(environ, start_response):
            """Commit headers explicitly, then attempt a prohibited error replacement."""
            write = start_response("200 OK", [("Content-Type", "text/plain")])
            write(b"")
            try:
                raise ValueError("already committed")
            except ValueError:
                start_response("500 Error", [], sys.exc_info())
            return []

        with self.assertRaisesRegex(ValueError, "already committed"):
            WSGIApplication(application)(Request("GET", "/"))

    def test_error_after_yield_aborts_and_closes_iterable(self):
        """A generator's partial output is discarded and its cleanup runs on failure."""
        closed = []

        def application(environ, start_response):
            """Replace a response after yielding data and expose generator cleanup."""
            start_response("200 OK", [("Content-Type", "text/plain")])
            try:
                yield b"partial"
                try:
                    raise ValueError("after yield")
                except ValueError:
                    start_response("500 Error", [], sys.exc_info())
                yield b"error"
            finally:
                closed.append(True)

        with self.assertRaisesRegex(ValueError, "after yield"):
            WSGIApplication(application)(Request("GET", "/"))
        self.assertEqual(closed, [True])


class GatewayTimeoutTests(unittest.TestCase):
    def test_incomplete_upload_closes_connection_and_next_request_succeeds(self):
        """A timed-out upload cannot leave its socket reusable with unread body bytes."""

        class LocalClient:
            def request(self, request):
                """Reply locally after the gateway has received a complete HTTP request."""
                return Response.text(200, "ok")

        server = Gateway(("127.0.0.1", 0), LocalClient(), limits=Limits(timeout=0.15))
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        connection = HTTPConnection(*server.server_address, timeout=3)
        try:
            connection.putrequest("POST", "/")
            connection.putheader("Content-Length", "10")
            connection.endheaders(b"x")
            response = connection.getresponse()
            self.assertEqual(response.status, 504)
            self.assertEqual(response.getheader("Connection"), "close")
            response.read()
            self.assertIsNone(connection.sock)
            connection.request("GET", "/")
            response = connection.getresponse()
            self.assertEqual((response.status, response.read()), (200, b"ok"))
        finally:
            connection.close()
            server.shutdown()
            server.server_close()
            worker.join(timeout=3)
