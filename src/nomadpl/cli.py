"""Command-line entry points for hosting and browsing NomadPL sites."""

import argparse
import importlib
import logging
import signal
import threading
import time
import tomllib
from pathlib import Path

from .gateway import Gateway
from .http import Limits
from .static import StaticSite
from .transport import Client, Site, load_identity
from .urls import URL
from .wsgi import WSGIApplication


def _build_parser():
    """Define the hosting and browser commands without starting Reticulum."""
    parser = argparse.ArgumentParser(
        description="NomadPL: standard web content on nomadpl.node destinations"
    )
    parser.add_argument(
        "--rns-config",
        help="Reticulum configuration directory (default: normal shared instance)",
    )
    parser.add_argument("--timeout", type=float, default=120)
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve", help="Serve static files or a WSGI application")
    serve.add_argument("--root", default="public")
    serve.add_argument("--identity", default="state/identity")
    serve.add_argument("--name", default="NomadPL")
    serve.add_argument("--app", help="WSGI callable as module:attribute")
    serve.add_argument(
        "--sites", help="TOML file containing multiple [[sites]] definitions"
    )
    serve.add_argument("--announce-interval", type=float, default=3600)
    browse = sub.add_parser(
        "browse", help="Open a loopback HTTP gateway to one destination"
    )
    browse.add_argument("url", help="nomadpl://<destination>/path or destination hash")
    browse.add_argument("--port", type=int, default=8080)
    return parser


def _run_gateway(args, limits):
    """Serve one remote site through a loopback-only browser origin."""
    url = URL.parse(args.url if "://" in args.url else "nomadpl://" + args.url + "/")
    client = Client(url.destination, limits=limits)
    hostname = url.destination + ".localhost"
    gateway = Gateway(
        ("127.0.0.1", args.port), client, limits=limits, canonical_host=hostname
    )
    print(
        f"Open http://{hostname}:{gateway.server_address[1]}{url.target}"
        + ("#" + url.fragment if url.fragment else ""),
        flush=True,
    )
    try:
        gateway.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        gateway.server_close()
        client.close()


def _run_sites(args, limits, parser):
    """Load configured identities and keep their site announcements running."""
    if args.announce_interval < 60:
        parser.error("announce interval must be at least 60 seconds")
    if args.sites:
        path = Path(args.sites).resolve()
        config = tomllib.loads(path.read_text(encoding="utf-8"))
        definitions, base = config["sites"], path.parent
    else:
        definitions = [
            {
                "root": args.root,
                "identity": args.identity,
                "name": args.name,
                "app": args.app,
            }
        ]
        base = Path.cwd()
    # Check every identity against every static root before loading or announcing keys.
    identity_paths = [(base / item["identity"]).resolve() for item in definitions]
    roots = [
        (base / item["root"]).resolve() for item in definitions if not item.get("app")
    ]
    for identity_path in identity_paths:
        if any(identity_path.is_relative_to(root) for root in roots):
            raise ValueError("Identity files must be outside every public static root")
    sites, identities = [], set()
    for definition in definitions:
        identity_path = (base / definition["identity"]).resolve()
        if identity_path in identities:
            raise ValueError("Each site must have a separate identity file")
        identities.add(identity_path)
        if definition.get("app"):
            module, attr = definition["app"].split(":", 1)
            app = WSGIApplication(
                getattr(importlib.import_module(module), attr), limits=limits
            )
        else:
            app = StaticSite(base / definition["root"], limits=limits)
        site = Site(
            load_identity(identity_path),
            app,
            name=definition.get("name", "NomadPL"),
            limits=limits,
        )
        if any(existing.address == site.address for existing in sites):
            raise ValueError("Sites have duplicate identity keys")
        sites.append(site)
        print(f"{site.name}: nomadpl://{site.address}/", flush=True)
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    next_announce = time.monotonic() + 3
    while not stop.wait(0.5):
        if time.monotonic() >= next_announce:
            for site in sites:
                site.announce()
            next_announce = time.monotonic() + args.announce_interval


def main():
    """Parse options, start Reticulum once, and run the selected command."""
    parser = _build_parser()
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("timeout must be positive")
    limits = Limits(timeout=args.timeout)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    import RNS

    RNS.Reticulum(configdir=args.rns_config)
    if args.command == "browse":
        _run_gateway(args, limits)
    else:
        _run_sites(args, limits, parser)


if __name__ == "__main__":
    main()
