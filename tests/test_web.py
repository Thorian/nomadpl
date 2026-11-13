import tempfile
import threading
import unittest
from http.client import HTTPConnection
from pathlib import Path
from wsgiref.validate import validator

from nomadpl import URL, Limits, Request, Response, StaticSite, WSGIApplication
from nomadpl.gateway import Gateway
from nomadpl.transport import Site


class StaticTests(unittest.TestCase):
    def setUp(self):
        """Create isolated temporary state for this test's assertions."""
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "index.html").write_bytes(b"<h1>Hello</h1>")
        (self.root / "dir").mkdir()
        (self.root / "dir" / "index.html").write_bytes(b"directory")
        (self.root / "style.css").write_bytes(b"body{color:red}")
        (self.root / "photo.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\xff")
        (self.root / "hello world.html").write_bytes(b"space")
        self.app = StaticSite(self.root)

    def test_content_types_and_binary_are_preserved(self):
        """Keep asset MIME types and binary bytes intact through static delivery."""
        for path, mime in [
            ("/", "text/html"),
            ("/style.css", "text/css"),
            ("/photo.png", "image/png"),
        ]:
            response = self.app(Request("GET", path))
            self.assertEqual(response.status, 200)
            self.assertTrue(dict(response.headers)["Content-Type"].startswith(mime))
        self.assertEqual(self.app(Request("GET", "/photo.png")).body[-2:], b"\x00\xff")

    def test_urls_query_redirect_index_and_encoding(self):
        """Preserve encoded filenames and queries when resolving directory indexes."""
        self.assertEqual(
            self.app(Request("GET", "/hello%20world.html?x=1&x=2")).body, b"space"
        )
        redirect = self.app(Request("GET", "/dir?q=%26"))
        self.assertEqual(
            (redirect.status, dict(redirect.headers)["Location"]), (308, "/dir/?q=%26")
        )
        self.assertEqual(self.app(Request("GET", "/dir/")).body, b"directory")

    def test_cache_head_range_and_missing(self):
        """Check conditional responses, HEAD framing, ranges and missing resources."""
        get = self.app(Request("GET", "/"))
        head = self.app(Request("HEAD", "/"))
        self.assertEqual(head.body, b"")
        self.assertEqual(dict(head.headers)["Content-Length"], str(len(get.body)))
        etag = dict(get.headers)["ETag"]
        self.assertEqual(
            self.app(Request("GET", "/", [("If-None-Match", "W/" + etag)])).status, 304
        )
        date = dict(get.headers)["Last-Modified"]
        self.assertEqual(
            self.app(Request("GET", "/", [("If-Modified-Since", date)])).status, 304
        )
        part = self.app(Request("GET", "/photo.png", [("Range", "bytes=1-3")]))
        self.assertEqual((part.status, part.body), (206, b"PNG"))
        self.assertEqual(
            self.app(Request("GET", "/photo.png", [("Range", "bytes=-2")])).body,
            b"\x00\xff",
        )
        self.assertEqual(
            self.app(Request("GET", "/photo.png", [("Range", "bytes=999-")])).status,
            416,
        )
        self.assertEqual(self.app(Request("GET", "/missing")).status, 404)
        self.assertEqual(self.app(Request("POST", "/")).status, 405)

    def test_private_files_and_path_escape_are_denied(self):
        """Keep private pages and traversal attempts outside the public file surface."""
        (self.root / ".env").write_text("private")
        (self.root / "private.html").write_text("private")
        (self.root / "private.html.allowed").touch()
        for target in (
            "/%2e%2e/private.html",
            "/.env",
            "/private.html",
            "/private.html.allowed",
            "/C:/windows",
        ):
            self.assertEqual(self.app(Request("GET", target)).status, 403, target)
        for target in ("/bad%00.html", "/bad%5cfile"):
            self.assertEqual(self.app(Request("GET", target)).status, 400)

    def test_symlink_escape(self):
        """Refuse a public-looking symlink that resolves outside the configured root."""
        with tempfile.TemporaryDirectory() as outside:
            external = Path(outside) / "secret.html"
            external.write_text("secret")
            try:
                (self.root / "linked.html").symlink_to(external)
            except OSError:
                self.skipTest("OS does not permit symlinks")
            self.assertEqual(self.app(Request("GET", "/linked.html")).status, 403)

    def test_file_size_limit(self):
        """Reject oversized static files before returning their contents."""
        response = StaticSite(self.root, limits=Limits(response_body=5))(
            Request("GET", "/")
        )
        self.assertEqual(response.status, 413)


class ProtocolTests(unittest.TestCase):
    def test_url_resolution(self):
        """Resolve relative references without losing query strings or fragments."""
        base = URL.parse("nomadpl://" + "ab" * 16 + "/docs/page.html?q=1#old")
        self.assertEqual(
            base.resolve("../assets/style.css").target, "/assets/style.css"
        )
        self.assertEqual(
            base.resolve("?q=2&q=3#next").target, "/docs/page.html?q=2&q=3"
        )
        self.assertEqual(base.resolve("#next").fragment, "next")
        for bad in (
            "nomadpl://user@" + "ab" * 16,
            "nomadpl://" + "ab" * 16 + ":80/",
            "http://example.com/",
        ):
            with self.assertRaises(ValueError):
                URL.parse(bad)

    def test_wire_validation_and_repeated_headers(self):
        """
        Preserve repeated fields while rejecting malformed request and header data.
        """
        response = Response(
            200, [("Set-Cookie", "a=1"), ("Set-Cookie", "b=2")], b"\x00\xff"
        )
        self.assertEqual(Response.from_wire(response.to_wire()), response)
        for target in ("//evil/a", "/%xx", "/a#fragment", "/a\r\nb", "https://evil/"):
            with self.assertRaises(ValueError):
                Request("GET", target).validate()
        with self.assertRaises(ValueError):
            Response(200, [("X-Test", "bad\r\nInjected: yes")]).validate()

    def test_site_reports_invalid_envelope(self):
        """Return a protocol error for an incomplete web request envelope."""
        site = Site.__new__(Site)
        site.limits = Limits()
        response = site._respond("/nomadpl/http", {}, None, None, None, None)
        self.assertEqual(response["status"], 400)

    def test_wsgi_forms_methods_headers_and_cleanup(self):
        """
        Preserve WSGI input and repeated headers, then close the response iterable.
        """
        closed = []

        def application(environ, start_response):
            """Produce the controlled WSGI response used by this protocol test."""
            self.assertEqual(environ["REQUEST_METHOD"], "POST")
            self.assertEqual(environ["QUERY_STRING"], "a=1&a=2")
            self.assertEqual(environ["PATH_INFO"], "/caf\xc3\xa9")
            self.assertEqual(
                environ["wsgi.input"].read(int(environ["CONTENT_LENGTH"])),
                b"name=A%26B",
            )
            start_response(
                "201 Created",
                [
                    ("Content-Type", "text/plain"),
                    ("Set-Cookie", "a=1"),
                    ("Set-Cookie", "b=2"),
                ],
            )
            try:
                yield b"saved"
            finally:
                closed.append(True)

        app = WSGIApplication(validator(application))
        response = app(
            Request(
                "POST",
                "/caf%C3%A9?a=1&a=2",
                [("Content-Type", "application/x-www-form-urlencoded")],
                b"name=A%26B",
            )
        )
        self.assertEqual((response.status, response.body), (201, b"saved"))
        self.assertEqual(sum(k == "Set-Cookie" for k, _ in response.headers), 2)
        self.assertEqual(closed, [True])

    def test_wsgi_write_and_iterable_order(self):
        """Keep explicit WSGI writes in order with yielded response chunks."""

        def application(environ, start_response):
            """Produce the controlled WSGI response used by this protocol test."""
            write = start_response("200 OK", [("Content-Type", "text/plain")])
            write(b"one")
            yield b"two"
            write(b"three")
            yield b"four"

        self.assertEqual(
            WSGIApplication(application)(Request("GET", "/")).body, b"onetwothreefour"
        )


class GatewayTests(unittest.TestCase):
    def setUp(self):
        """Create isolated temporary state for this test's assertions."""
        self.requests = []
        owner = self

        class FakeClient:
            def request(self, request):
                """
                Return a predictable response while recording the gateway's forwarded
                request.
                """
                owner.requests.append(request)
                if request.target == "/redirect":
                    return Response(302, [("Location", "/next?q=1#anchor")])
                return Response(
                    200,
                    [
                        ("Content-Type", "application/octet-stream"),
                        ("Set-Cookie", "a=1"),
                        ("Set-Cookie", "b=2"),
                    ],
                    request.body or b"\x00\xff",
                )

        self.server = Gateway(("127.0.0.1", 0), FakeClient())
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def connect(self):
        """Open a local test connection and arrange for it to close afterward."""
        connection = HTTPConnection(*self.server.server_address, timeout=3)
        self.addCleanup(connection.close)
        return connection

    def test_http_bytes_query_forms_and_cookies(self):
        """Pass binary bodies, form targets and repeated cookies through the gateway."""
        connection = self.connect()
        connection.request(
            "POST",
            "/submit?a=1&a=2",
            b"form=hello",
            {"Content-Type": "application/x-www-form-urlencoded"},
        )
        response = connection.getresponse()
        self.assertEqual((response.status, response.read()), (200, b"form=hello"))
        self.assertEqual(
            len([k for k, v in response.getheaders() if k == "Set-Cookie"]), 2
        )
        self.assertEqual(self.requests[0].target, "/submit?a=1&a=2")
        connection.request("GET", "/redirect")
        response = connection.getresponse()
        self.assertEqual(
            (response.status, response.getheader("Location")), (302, "/next?q=1#anchor")
        )
        response.read()

    def test_rejects_foreign_hosts_and_cross_origin_access(self):
        """Block browser requests that do not belong to the gateway's local origin."""
        connection = self.connect()
        connection.request("GET", "/", headers={"Host": "evil.example"})
        response = connection.getresponse()
        self.assertEqual(response.status, 421)
        response.read()
        connection = self.connect()
        connection.request("POST", "/", headers={"Origin": "https://evil.example"})
        response = connection.getresponse()
        self.assertEqual(response.status, 403)
        response.read()
        self.assertEqual(self.requests, [])

    def test_head_and_chunked_upload(self):
        """Suppress HEAD bodies and reject unsupported chunked request framing."""
        connection = self.connect()
        connection.request("HEAD", "/")
        response = connection.getresponse()
        self.assertEqual((response.status, response.read()), (200, b""))
        connection.request("POST", "/", headers={"Transfer-Encoding": "chunked"})
        response = connection.getresponse()
        self.assertEqual(response.status, 501)
        response.read()

    def test_per_site_hostname_is_enforced(self):
        """
        Require the configured hostname before forwarding requests to a remote site.
        """
        self.server.canonical_host = "ab" * 16 + ".localhost"
        connection = self.connect()
        connection.request("GET", "/")
        response = connection.getresponse()
        self.assertEqual(response.status, 421)
        response.read()
        connection = self.connect()
        host = self.server.canonical_host + ":" + str(self.server.server_address[1])
        connection.request(
            "GET", "/", headers={"Host": host, "Origin": "http://" + host}
        )
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        response.read()
