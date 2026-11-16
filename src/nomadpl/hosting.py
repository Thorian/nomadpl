"""Configuration and lifecycle for a headless, multi-site page host."""

import math
import os
import re
import shutil
import signal
import threading
import time
import tomllib
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .external import Interpreter
from .http import Limits
from .nomadnet import NomadNetSite
from .pages import PageApplication
from .transport import Site, load_identity


@dataclass
class HostedSite:
    name: str
    identity: Path
    pages: dict


@dataclass
class HostConfig:
    state: Path
    sites: list[HostedSite]
    announce_interval: float
    max_requests: int
    request_rate: float
    request_burst: int


def positive_number(value, name, *, minimum=0):
    """Validate finite operator-supplied durations and admission settings."""
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= minimum
    ):
        raise ValueError(f"{name} must be finite and greater than {minimum}")
    return value


def load_config(path, *, limits=Limits()):
    """Validate every root, key path and interpreter before starting network services."""
    path = Path(path).resolve(strict=True)
    base = path.parent
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    state = (base / data.get("state", "state")).resolve()
    interval = positive_number(
        data.get("announce_interval", 3600), "announce_interval", minimum=59
    )
    timeout = positive_number(data.get("script_timeout", 30), "script_timeout")
    max_requests = data.get("max_requests", 8)
    burst = data.get("request_burst", 16)
    if (
        type(max_requests) is not int
        or max_requests <= 0
        or type(burst) is not int
        or burst <= 0
    ):
        raise ValueError("Request counts must be positive integers")
    rate = positive_number(data.get("request_rate", 8), "request_rate")
    interpreters = {}
    for name, item in data.get("interpreters", {}).items():
        command, suffixes = item.get("command"), item.get("suffixes")
        if not isinstance(command, list) or not isinstance(suffixes, list):
            raise ValueError("Each interpreter needs command and suffixes lists")
        interpreter = Interpreter(
            tuple(command), tuple(suffixes), item.get("output", "cgi")
        )
        executable = shutil.which(interpreter.command[0])
        if executable is None:
            raise ValueError(f"Interpreter not found: {interpreter.command[0]}")
        if Path(executable).suffix.lower() in (".bat", ".cmd"):
            raise ValueError("Use an interpreter executable, not a shell command file")
        interpreters[name] = Interpreter(
            (executable, *interpreter.command[1:]),
            interpreter.suffixes,
            interpreter.output,
        )
    definitions = data.get("sites")
    if not isinstance(definitions, list) or not definitions:
        raise ValueError("Configure at least one site")
    sites, identifiers, keys = [], set(), set()
    for item in definitions:
        identifier = item["id"]
        if (
            not isinstance(identifier, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", identifier)
            or identifier in identifiers
        ):
            raise ValueError("Site ids must be unique simple names")
        identifiers.add(identifier)
        name = item.get("name", identifier)
        if (
            not isinstance(name, str)
            or not 1 <= len(name.encode()) <= 128
            or any(ord(char) < 32 for char in name)
        ):
            raise ValueError("Site names must be 1–128 UTF-8 bytes without controls")
        identity = (
            (base / item["identity"]).resolve()
            if "identity" in item
            else state / (identifier + ".identity")
        )
        if not identity.is_relative_to(state) or identity == state or identity in keys:
            raise ValueError(
                "Each site needs a unique identity file inside the state directory"
            )
        keys.add(identity)
        pages = {}
        for protocol in ("nomadpl", "nomadnet"):
            if protocol not in item:
                continue
            settings = item[protocol]
            selected = settings.get("interpreters", [])
            if not isinstance(selected, list) or any(
                name not in interpreters for name in selected
            ):
                raise ValueError("Unknown page interpreter")
            require_identity = settings.get("require_identity", False)
            if type(require_identity) is not bool:
                raise ValueError("require_identity must be true or false")
            index = settings.get(
                "index",
                ["index.mu"] if protocol == "nomadnet" else ["index.html", "index.php"],
            )
            if not isinstance(index, list) or any(
                not isinstance(name, str) for name in index
            ):
                raise ValueError("index must be a list of filenames")
            pages[protocol] = PageApplication(
                base / settings["root"],
                interpreters=[interpreters[name] for name in selected],
                index=index,
                require_identity=require_identity,
                limits=limits,
                script_timeout=timeout,
                protocol=protocol,
            )
        if not pages:
            raise ValueError("Each site must enable NomadPL, NomadNet or both")
        sites.append(HostedSite(name, identity, pages))
    for site in sites:
        for app in site.pages.values():
            if (
                state.is_relative_to(app.root)
                or app.root.is_relative_to(state)
                or path.is_relative_to(app.root)
            ):
                raise ValueError(
                    "Public roots must be separate from state and the host configuration"
                )
    return HostConfig(state, sites, interval, max_requests, rate, burst)


@contextmanager
def state_lock(directory):
    """Hold a process lock so two hosts cannot share the same identity state."""
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    stream = (directory / ".host.lock").open("a+b")
    locked = False
    try:
        if os.name == "nt":
            import msvcrt

            stream.seek(0)
            if not stream.read(1):
                stream.write(b"0")
                stream.flush()
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        locked = True
        yield
    except OSError as exc:
        if not locked:
            raise RuntimeError("Another host is using this state directory") from exc
        raise
    finally:
        if locked:
            if os.name == "nt":
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)
        stream.close()


class Host:
    def __init__(self, config):
        """Load one persistent key per site and register each configured protocol."""
        self.sites = []
        identities = set()
        for definition in config.sites:
            identity = load_identity(definition.identity)
            if identity.hash in identities:
                raise ValueError("Sites contain duplicate identity keys")
            identities.add(identity.hash)
            for protocol, app in definition.pages.items():
                factory = Site if protocol == "nomadpl" else NomadNetSite
                options = {
                    "name": definition.name,
                    "max_requests": config.max_requests,
                    "request_rate": config.request_rate,
                    "request_burst": config.request_burst,
                }
                if protocol == "nomadpl":
                    options["limits"] = app.limits
                site = factory(identity, app, **options)
                self.sites.append(site)
                address = (
                    f"nomadpl://{site.address}/"
                    if protocol == "nomadpl"
                    else f"{site.address}:/page/index.mu"
                )
                print(f"{definition.name} ({protocol}): {address}", flush=True)

    def announce(self):
        """Announce all configured destinations from the main host loop."""
        for site in self.sites:
            site.announce()

    def close(self):
        """Stop admitting requests and give in-flight page handlers time to finish."""
        for site in self.sites:
            site._gate.stop()
        for site in self.sites:
            site._gate.wait_idle(site.app.script_timeout + 5)


def run_host(config):
    """Run without a terminal UI, stopping cleanly on SIGINT or SIGTERM."""
    stop = threading.Event()
    previous = {}
    for number in (signal.SIGINT, signal.SIGTERM):
        previous[number] = signal.signal(number, lambda *_: stop.set())
    host = None
    try:
        host = Host(config)
        next_announce = time.monotonic() + 3
        while not stop.wait(0.5):
            if time.monotonic() >= next_announce:
                host.announce()
                next_announce = time.monotonic() + config.announce_interval
    finally:
        if host is not None:
            host.close()
        for number, handler in previous.items():
            signal.signal(number, handler)
