"""Subprocess fixture used by test_network.py; requires the real RNS stack."""

import json
import os
import shutil
import sys
import threading
import time
from http.client import HTTPConnection
from pathlib import Path

import RNS

from nomadpl import Client, Request, Site, StaticSite, WSGIApplication
from nomadpl.gateway import Gateway
from nomadpl.hosting import Host, load_config
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
    # Exercise the actual host with a fresh key and both page protocols.
    host_root = directory / "host"
    for name in ("html", "micron"):
        (host_root / name).mkdir(parents=True)
    (host_root / "micron" / "index.mu").write_text(">Hosted Micron page\n")
    interpreter = os.environ.get("NOMADPL_PHP_CGI") or shutil.which("php-cgi")
    if interpreter:
        command, suffix = [str(interpreter)], ".php"
        source = """<?php
header("Content-Type: application/json");
echo json_encode(["user"=>$_SERVER["REMOTE_USER"], "auth"=>$_SERVER["AUTH_TYPE"],
 "get"=>$_GET, "post"=>$_POST, "method"=>$_SERVER["REQUEST_METHOD"]]);
"""
    else:
        command, suffix = [sys.executable, "{script}"], ".cgi"
        source = """import json, os, sys
from urllib.parse import parse_qs
print("Content-Type: application/json\\n")
print(json.dumps({"user": os.environ["REMOTE_USER"], "auth": os.environ["AUTH_TYPE"],
 "get": {k: v[0] for k,v in parse_qs(os.environ["QUERY_STRING"]).items()},
 "post": {k: v[0] for k,v in parse_qs(sys.stdin.read()).items()},
 "method": os.environ["REQUEST_METHOD"]}))
"""
    visitor = load_identity(directory / "visitor.identity")
    for name in ("html", "micron"):
        (host_root / name / ("form" + suffix)).write_text(source)
        (host_root / name / ("private" + suffix)).write_text(source)
        (host_root / name / ("private" + suffix + ".allowed")).write_text(
            visitor.hash.hex()
        )
    (host_root / "host.toml").write_text(
        "[interpreters.script]\ncommand="
        + json.dumps(command)
        + '\nsuffixes=["'
        + suffix
        + '"]\n'
        '[[sites]]\nid="hosted"\nname="Host test"\n'
        '[sites.nomadpl]\nroot="html"\ninterpreters=["script"]\n'
        '[sites.nomadnet]\nroot="micron"\ninterpreters=["script"]\n'
    )
    hosted = Host(load_config(host_root / "host.toml"))
    assert static.address != dynamic.address
    (directory / "ready.json").write_text(
        json.dumps(
            {
                "static": static.address,
                "dynamic": dynamic.address,
                "hosted_web": hosted.sites[0].address,
                "hosted_micron": hosted.sites[1].address,
                "suffix": suffix,
            }
        )
    )
    next_announce = 0
    while not (directory / "stop").exists():
        if time.monotonic() > next_announce:
            static.announce()
            dynamic.announce()
            hosted.announce()
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
        actor = load_identity(directory / "visitor.identity")
        with_identity = Client(addresses["hosted_web"], identity=actor)
        anonymous = Client(addresses["hosted_web"])
        form = "/form" + addresses["suffix"]
        try:
            payload = b"name=A%26B+%2B+caf%C3%A9"
            response = with_identity.request(
                Request(
                    "POST",
                    form + "?view=profile",
                    [
                        ("Content-Type", "application/x-www-form-urlencoded"),
                        ("Remote-User", "forged"),
                    ],
                    payload,
                )
            )
            data = json.loads(response.body)
            assert data == {
                "user": actor.hash.hex(),
                "auth": "Reticulum",
                "get": {"view": "profile"},
                "post": {"name": "A&B + café"},
                "method": "POST",
            }, data
            response = anonymous.request(
                Request("GET", form, [("Remote-User", actor.hash.hex())])
            )
            assert json.loads(response.body)["user"] == ""
            assert (
                anonymous.request(
                    Request(
                        "GET",
                        "/private" + addresses["suffix"],
                        [("Remote-User", actor.hash.hex())],
                    )
                ).status
                == 403
            )
            assert (
                with_identity.request(
                    Request("GET", "/private" + addresses["suffix"])
                ).status
                == 200
            )
        finally:
            with_identity.close()
            anonymous.close()

        address = bytes.fromhex(addresses["hosted_micron"])
        deadline = time.monotonic() + 30
        RNS.Transport.request_path(address)
        while not RNS.Transport.has_path(address):
            assert time.monotonic() < deadline, "No NomadNet path"
            time.sleep(0.05)
        destination = RNS.Destination(
            RNS.Identity.recall(address),
            RNS.Destination.OUT,
            RNS.Destination.SINGLE,
            "nomadnetwork",
            "node",
        )
        assert destination.hash == address
        link = RNS.Link(destination)
        try:
            while link.status != RNS.Link.ACTIVE:
                assert time.monotonic() < deadline, "No NomadNet link"
                time.sleep(0.05)
            link.identify(actor)
            receipt = link.request(
                "/page" + form,
                data={
                    "field_name": "A&B + café",
                    "var_view": "profile",
                    "field_REMOTE_USER": "forged",
                },
                timeout=30,
            )
            while receipt.status not in (
                RNS.RequestReceipt.READY,
                RNS.RequestReceipt.FAILED,
            ):
                assert time.monotonic() < deadline, "No NomadNet response"
                time.sleep(0.05)
            assert receipt.status == RNS.RequestReceipt.READY, "NomadNet request failed"
            data = json.loads(receipt.response)
            assert data["user"] == actor.hash.hex() and data["auth"] == "Reticulum", (
                data
            )
            assert (
                data["get"] == {"view": "profile"}
                and data["post"]["name"] == "A&B + café"
            ), data
        finally:
            link.teardown()
        print("NETWORK_SMOKE_OK", flush=True)
    finally:
        static.close()
        dynamic.close()
