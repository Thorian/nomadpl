"""Subprocess fixture used by test_network.py; requires the real RNS stack."""

import json
import sys
import threading
import time
from http.client import HTTPConnection
from pathlib import Path

import RNS

from nomadpl import Client, Request, Site, StaticSite, WSGIApplication
from nomadpl.gateway import Gateway
from nomadpl.transport import load_identity


def app(environ, start_response):
    """Provide a dynamic test site that echoes forms and issues a redirect."""
    if environ["PATH_INFO"] == "/redirect":
        start_response("303 See Other", [("Location", "/echo?q=redirect#anchor")])
        return [b""]
    length = int(environ["CONTENT_LENGTH"])
    data = json.dumps(
        {
            "method": environ["REQUEST_METHOD"],
            "query": environ["QUERY_STRING"],
            "body": environ["wsgi.input"].read(length).decode(),
        }
    ).encode()
    start_response(
        "200 OK",
        [
            ("Content-Type", "application/json"),
            ("Set-Cookie", "session=abc; Path=/; SameSite=Lax"),
        ],
    )
    return [data]


mode, directory = sys.argv[1], Path(sys.argv[2])
RNS.Reticulum(configdir=str(directory / mode))
if mode == "server":
    identity = load_identity(directory / "static.identity")
    assert identity.hash == load_identity(directory / "static.identity").hash
    static = Site(identity, StaticSite(directory / "www"), name="HTML test")
    nomadnet = RNS.Destination(
        identity, RNS.Destination.IN, RNS.Destination.SINGLE, "nomadnetwork", "node"
    )
    assert static.address != nomadnet.hash.hex()
    assert static.destination.hash == RNS.Destination.hash(identity, "nomadpl", "node")
    assert len(static.destination.request_handlers) == 1
    assert (
        RNS.Identity.truncated_hash(b"/page/index.html")
        not in static.destination.request_handlers
    )
    dynamic = Site(
        load_identity(directory / "dynamic.identity"),
        WSGIApplication(app),
        name="WSGI test",
    )
    assert static.address != dynamic.address
    (directory / "ready.json").write_text(
        json.dumps({"static": static.address, "dynamic": dynamic.address})
    )
    next_announce = 0
    while not (directory / "stop").exists():
        if time.monotonic() > next_announce:
            static.announce()
            dynamic.announce()
            next_announce = time.monotonic() + 2
        time.sleep(0.1)
else:
    addresses = json.loads((directory / "ready.json").read_text())
    static = Client(addresses["static"])
    dynamic = Client(addresses["dynamic"])
    try:
        gateway = Gateway(("127.0.0.1", 0), static)
        threading.Thread(target=gateway.serve_forever, daemon=True).start()
        try:
            connection = HTTPConnection(*gateway.server_address, timeout=50)
            for path, mime in [
                ("/?a=1&a=2", "text/html"),
                ("/assets/style.css", "text/css"),
                ("/assets/image.png", "image/png"),
            ]:
                connection.request("GET", path)
                response = connection.getresponse()
                body = response.read()
                assert response.status == 200, (path, response.status, body)
                assert response.getheader("Content-Type").startswith(mime)
                if path.endswith(".png"):
                    assert body == b"\x89PNG\r\n\x1a\n\x00\xff"
            connection.request(
                "GET", "/assets/image.png", headers={"Range": "bytes=1-3"}
            )
            response = connection.getresponse()
            assert (response.status, response.read()) == (206, b"PNG")
            connection.close()
        finally:
            gateway.shutdown()
            gateway.server_close()
        payload = b"name=A%26B&second=two"
        response = dynamic.request(
            Request(
                "POST",
                "/echo?a=1&a=2",
                [("Content-Type", "application/x-www-form-urlencoded")],
                payload,
            )
        )
        assert response.status == 200
        assert json.loads(response.body) == {
            "method": "POST",
            "query": "a=1&a=2",
            "body": payload.decode(),
        }
        assert ("Set-Cookie", "session=abc; Path=/; SameSite=Lax") in response.headers
        response = dynamic.request(Request("GET", "/redirect"))
        assert (
            response.status == 303
            and dict(response.headers)["Location"] == "/echo?q=redirect#anchor"
        )
        # A large body exercises Reticulum Resources instead of a single packet.
        response = static.request(Request("GET", "/large.bin"))
        assert len(response.body) == 65536 and response.body == bytes(range(256)) * 256
        print("NETWORK_SMOKE_OK", flush=True)
    finally:
        static.close()
        dynamic.close()
