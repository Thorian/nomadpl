"""Page host tests with real interpreters and temporary identities."""

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import RNS

from nomadpl import Limits, Request, StaticSite, WSGIApplication
from nomadpl.admission import RequestGate
from nomadpl.external import Interpreter, cgi_environment, parse_output
from nomadpl.hosting import load_config, state_lock
from nomadpl.nomadnet import page_request
from nomadpl.pages import PageApplication
from nomadpl.transport import Site

PHP = os.environ.get("NOMADPL_PHP_CGI") or shutil.which("php-cgi")
IDENTITY = bytes.fromhex("12" * 16)


class HostTests(unittest.TestCase):
    def setUp(self):
        """Create a public directory separate from test configuration and keys."""
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.root = self.base / "public"
        self.root.mkdir()

    def test_config_checks_roots_before_creating_state(self):
        """Refuse a public root containing configuration or another site's private state."""
        path = self.base / "host.toml"
        path.write_text('[[sites]]\nid="home"\n[sites.nomadpl]\nroot="public"\n')
        config = load_config(path)
        self.assertEqual(
            config.sites[0].identity, self.base / "state" / "home.identity"
        )
        self.assertFalse(config.state.exists())
        path.write_text('[[sites]]\nid="home"\n[sites.nomadpl]\nroot="."\n')
        with self.assertRaisesRegex(ValueError, "separate"):
            load_config(path)

    def test_unknown_or_missing_interpreter_is_a_startup_error(self):
        """Do not silently serve source when an interpreter cannot be started."""
        path = self.base / "host.toml"
        path.write_text(
            '[[sites]]\nid="home"\n[sites.nomadpl]\nroot="public"\ninterpreters=["missing"]\n'
        )
        with self.assertRaisesRegex(ValueError, "Unknown"):
            load_config(path)
        path.write_text(
            '[interpreters.php]\ncommand=["nonexistent-nomadpl-interpreter"]\nsuffixes=[".php"]\n[[sites]]\nid="home"\n[sites.nomadpl]\nroot="public"\ninterpreters=["php"]\n'
        )
        with self.assertRaisesRegex(ValueError, "not found"):
            load_config(path)

    def test_state_lock_rejects_a_second_host(self):
        """Prevent two host instances from loading the same site's private keys."""
        with state_lock(self.base / "state"):
            with self.assertRaisesRegex(RuntimeError, "Another host"):
                with state_lock(self.base / "state"):
                    self.fail("Second host obtained the lock")
        with state_lock(self.base / "state"):
            pass

    def test_shutdown_refuses_new_work_and_waits_for_active_handlers(self):
        """Drain admitted requests without letting shutdown admit new ones."""
        gate = RequestGate()
        with gate.admit() as accepted:
            self.assertTrue(accepted)
            gate.stop()
            self.assertFalse(gate.wait_idle(0))
            with gate.admit() as later:
                self.assertFalse(later)
        self.assertTrue(gate.wait_idle(0))

    def test_required_identity_denies_anonymous_pages(self):
        """A header claiming an identity does not satisfy site-wide identification."""
        (self.root / "index.html").write_text("private")
        app = PageApplication(self.root, require_identity=True)
        self.assertEqual(
            app(Request("GET", "/", [("Remote-User", IDENTITY.hex())])).status, 403
        )
        self.assertEqual(app(Request("GET", "/", remote_identity=IDENTITY)).status, 200)

    def test_source_private_state_and_encoded_traversal_are_denied(self):
        """Serve public assets while refusing interpreter source and private paths."""
        (self.root / "page.php").write_text("private source")
        (self.root / "asset.css").write_text("body {}")
        app = PageApplication(self.root)
        for target in (
            "/page.php",
            "/%2e%2e/private",
            "/state/key",
            "/.env",
            "/page.php.allowed",
        ):
            self.assertEqual(app(Request("GET", target)).status, 403)
        self.assertEqual(app(Request("GET", "/asset.css")).status, 200)

    def test_identity_allowlist_uses_local_transport_context(self):
        """Anonymous users and forged wire values cannot read an identity-restricted page."""
        (self.root / "index.html").write_text("private page")
        (self.root / "index.html.allowed").write_text(IDENTITY.hex())
        app = StaticSite(self.root)
        wire = Request("GET", "/index.html").to_wire()
        wire["remote_identity"] = IDENTITY
        self.assertEqual(app(Request.from_wire(wire)).status, 403)
        request = Request("GET", "/index.html", remote_identity=IDENTITY)
        self.assertEqual(app(request).body, b"private page")
        self.assertEqual(
            app(Request("GET", "/index.html.allowed", remote_identity=IDENTITY)).status,
            403,
        )
        (self.root / "index.html.allowed").write_text("invalid allowlist")
        self.assertEqual(app(request).status, 403)

    def test_nomadnet_forms_preserve_values_and_do_not_accept_identity_fields(self):
        """Map Micron field and link values without interpreting them as environment settings."""
        identity = RNS.Identity()
        request, variables = page_request(
            "/page/form.php",
            {
                "field_name": "A&B + café",
                "var_next": "a=b",
                "field_REMOTE_USER": "forged",
            },
            identity,
            b"link",
            Limits(),
        )
        environment = cgi_environment(
            request,
            self.root / "form.php",
            self.root,
            protocol="nomadnet",
            variables=variables,
        )
        self.assertEqual(request.method, "POST")
        self.assertIn(b"name=A%26B+%2B+caf%C3%A9", request.body)
        self.assertEqual(request.target, "/form.php?next=a%3Db")
        self.assertEqual(environment["REMOTE_USER"], identity.hash.hex())
        self.assertEqual(environment["field_name"], "A&B + café")
        with self.assertRaises(ValueError):
            page_request(
                "/page/form.php", {"REMOTE_USER": "forged"}, identity, None, Limits()
            )

    def test_wsgi_receives_verified_identity(self):
        """Keep the verified user separate from an HTTP header claiming another identity."""
        captured = {}

        def application(environ, start_response):
            """Save authentication values for assertions outside the WSGI callable."""
            captured.update(environ)
            start_response("200 OK", [])
            return [b"ok"]

        app = WSGIApplication(application)
        app(Request("GET", "/", [("Remote-User", "forged")], remote_identity=IDENTITY))
        self.assertEqual(captured["REMOTE_USER"], IDENTITY.hex())
        self.assertEqual(captured["AUTH_TYPE"], "Reticulum")

    def test_interpreter_timeout_and_output_limit(self):
        """Terminate stalled or noisy programs instead of hanging a request or filling memory."""
        script = self.root / "page.cgi"
        interpreter = Interpreter((sys.executable, "{script}"), (".cgi",))
        app = PageApplication(self.root, interpreters=[interpreter], script_timeout=0.2)
        script.write_text("import time\ntime.sleep(30)\n")
        with self.assertLogs(level="WARNING"):
            self.assertEqual(app(Request("GET", "/page.cgi")).status, 504)
        script.write_text('print("Content-Type: text/plain\\n\\n" + "x"*100000)\n')
        app = PageApplication(
            self.root,
            interpreters=[interpreter],
            limits=Limits(response_body=64),
            script_timeout=5,
        )
        with self.assertLogs(level="ERROR"):
            self.assertEqual(app(Request("GET", "/page.cgi")).status, 502)

    def test_cgi_does_not_inherit_secrets_or_proxy_settings(self):
        """Build only approved environment values and avoid HTTP_PROXY injection."""
        with patch.dict(
            os.environ, {"PRIVATE_SECRET": "hidden", "PHPRC": "/untrusted"}
        ):
            environment = cgi_environment(
                Request("GET", "/", [("Proxy", "evil"), ("Remote-User", "forged")]),
                self.root / "page.php",
                self.root,
            )
        self.assertNotIn("PRIVATE_SECRET", environment)
        self.assertNotIn("PHPRC", environment)
        self.assertNotIn("HTTP_PROXY", environment)
        self.assertEqual(environment["REMOTE_USER"], "")

    def test_cgi_preserves_repeated_headers_and_binary_body(self):
        """Keep cookies separate and select the first header boundary."""
        interpreter = Interpreter(("unused",), (".cgi",))
        response = parse_output(
            b"Status: 201 Created\nSet-Cookie: a=1\nSet-Cookie: b=2\n\n\x00\xff\r\n\r\n",
            interpreter,
        )
        self.assertEqual(response.status, 201)
        self.assertEqual(response.body, b"\x00\xff\r\n\r\n")
        self.assertEqual(
            response.headers, [("Set-Cookie", "a=1"), ("Set-Cookie", "b=2")]
        )


@unittest.skipUnless(PHP, "Install php-cgi or set NOMADPL_PHP_CGI")
class PHPTests(unittest.TestCase):
    def setUp(self):
        """Create a real PHP page reporting its parsed input and authenticated identity."""
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "index.php").write_text("""<?php
header("Content-Type: application/json");
header("Set-Cookie: one=1", false);
header("Set-Cookie: two=2", false);
echo json_encode(["get"=>$_GET, "post"=>$_POST, "user"=>$_SERVER["REMOTE_USER"],
 "auth"=>$_SERVER["AUTH_TYPE"], "compat"=>getenv("remote_identity"),
 "method"=>$_SERVER["REQUEST_METHOD"], "cookie"=>$_COOKIE,
 "upload"=>isset($_FILES["upload"]) ? file_get_contents($_FILES["upload"]["tmp_name"]) : null]);
""")
        self.interpreter = Interpreter((str(PHP),), (".php",))
        self.app = PageApplication(self.root, interpreters=[self.interpreter])

    def test_php_forms_cookies_and_identification(self):
        """Exercise PHP's own query, POST-array, cookie and identity handling."""
        wire = Request(
            "POST",
            "/?name=Alice%26Bob&list[]=one&list[]=two",
            [
                ("Content-Type", "application/x-www-form-urlencoded"),
                ("Cookie", "session=abc"),
                ("Remote-User", "forged"),
            ],
            b"message=caf%C3%A9+%2B+tea&items[]=x&items[]=y",
        ).to_wire()
        wire["remote_identity"] = "forged"
        identity = RNS.Identity()
        site = Site.__new__(Site)
        site.app, site.limits, site._gate = self.app, Limits(), RequestGate()
        response = site._respond(None, wire, None, None, identity, None)
        data = json.loads(response["body"])
        self.assertEqual(data["get"], {"name": "Alice&Bob", "list": ["one", "two"]})
        self.assertEqual(data["post"], {"message": "café + tea", "items": ["x", "y"]})
        self.assertEqual(data["cookie"], {"session": "abc"})
        self.assertEqual(data["user"], identity.hash.hex())
        self.assertEqual(data["compat"], identity.hash.hex())
        self.assertEqual(data["auth"], "Reticulum")
        self.assertEqual(
            len(
                [
                    header
                    for header in response["headers"]
                    if header[0].lower() == "set-cookie"
                ]
            ),
            2,
        )
        anonymous = site._respond(None, wire, None, None, None, None)
        self.assertEqual(json.loads(anonymous["body"])["user"], "")

    def test_php_multipart_upload(self):
        """Pass a multipart body unchanged so PHP populates POST and FILES."""
        body = b'--nomadpl\r\nContent-Disposition: form-data; name="title"\r\n\r\nHello\r\n--nomadpl\r\nContent-Disposition: form-data; name="upload"; filename="test.txt"\r\nContent-Type: text/plain\r\n\r\nfile contents\r\n--nomadpl--\r\n'
        response = self.app(
            Request(
                "POST",
                "/index.php",
                [("Content-Type", "multipart/form-data; boundary=nomadpl")],
                body,
            )
        )
        data = json.loads(response.body)
        self.assertEqual(data["post"]["title"], "Hello")
        self.assertEqual(data["upload"], "file contents")

    def test_nomadnet_fields_work_in_php(self):
        """Deliver NomadNet form fields to PHP with the same verified identity."""
        identity = RNS.Identity()
        request, variables = page_request(
            "/page/index.php",
            {"field_name": "A&B", "var_view": "profile"},
            identity,
            None,
            Limits(),
        )
        response = self.app(request, variables=variables)
        data = json.loads(response.body)
        self.assertEqual(data["get"], {"view": "profile"})
        self.assertEqual(data["post"], {"name": "A&B"})
        self.assertEqual(data["user"], identity.hash.hex())


class PHPAvailabilityTests(unittest.TestCase):
    def test_required_php_is_installed(self):
        """Make a required PHP CI run fail instead of silently skipping integration tests."""
        if os.environ.get("NOMADPL_REQUIRE_PHP") == "1":
            self.assertTrue(PHP, "php-cgi is required for this test run")
