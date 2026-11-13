"""Web exchanges on dedicated nomadpl.node destinations and RNS links."""

import logging
import threading
import time
from pathlib import Path

from .admission import RequestGate
from .http import Limits, Request, Response

REQUEST_PATH = "/nomadpl/http"
ASPECTS = ("nomadpl", "node")


def load_identity(path):
    """Load a persistent key or create one without overwriting an existing file."""
    import RNS

    path = Path(path)
    if path.exists():
        identity = RNS.Identity.from_file(str(path))
        if identity is None:
            raise ValueError(f"Invalid identity file: {path}")
        return identity
    path.parent.mkdir(parents=True, exist_ok=True)
    identity = RNS.Identity()
    # Never overwrite an existing identity, including concurrent first starts.
    with path.open("xb") as stream:
        stream.write(identity.get_private_key())
    path.chmod(0o600)
    return identity


class Site:
    """Expose a web application on its own nomadpl.node destination."""

    def __init__(
        self,
        identity,
        app,
        *,
        name="NomadPL",
        limits=Limits(),
        max_requests=8,
        request_rate=8,
        request_burst=16,
    ):
        """Register the web exchange handler on a dedicated nomadpl.node destination."""
        import RNS

        if not 1 <= len(name.encode("utf-8")) <= 128:
            raise ValueError("Site name must be 1–128 UTF-8 bytes")
        self.app, self.name, self.limits = app, name, limits
        self._gate = RequestGate(max_requests, request_rate, request_burst)
        self.destination = RNS.Destination(
            identity, RNS.Destination.IN, RNS.Destination.SINGLE, *ASPECTS
        )
        self.destination.set_max_request_size(
            limits.request_body + limits.headers + limits.url + 8192
        )
        self.destination.register_request_handler(
            REQUEST_PATH,
            response_generator=self._respond,
            allow=RNS.Destination.ALLOW_ALL,
        )

    @property
    def address(self):
        """Return the site's destination hash in its shareable hexadecimal form."""
        return self.destination.hash.hex()

    def announce(self):
        # Announcements are scoped to nomadpl.node, separate from NomadNet.
        """Publish the site name under the NomadPL destination namespace."""
        self.destination.announce(app_data=self.name.encode("utf-8"))

    def _respond(self, path, data, request_id, link_id, remote_identity, requested_at):
        """Reject excess work before parsing requests or accessing application state."""
        with self._gate.admit() as accepted:
            if not accepted:
                return Response.text(503, "Site busy; try again later").to_wire()
            return self._handle_request(
                path, data, request_id, link_id, remote_identity, requested_at
            )

    def _handle_request(
        self, path, data, request_id, link_id, remote_identity, requested_at
    ):
        """
        Validate the exchange and turn application failures into bounded responses.
        """
        try:
            request = Request.from_wire(data, self.limits)
        except (ValueError, TypeError, KeyError):
            return Response.text(400, "Invalid NomadPL request").to_wire()
        try:
            response = self.app(request).validate(self.limits)
            if request.method == "HEAD" and not any(
                k.lower() == "content-length" for k, _ in response.headers
            ):
                response.headers.append(("Content-Length", str(len(response.body))))
            if request.method == "HEAD" or response.status in (204, 205, 304):
                response.body = b""
            return response.to_wire()
        except Exception:
            logging.exception("NomadPL application failed")
            return Response.text(500, "Application error").to_wire()


class Client:
    """Reusable, serialised link per origin. Create after RNS.Reticulum()."""

    def __init__(self, destination, *, limits=Limits(), identity=None):
        """Prepare a reusable client for one destination and its request limits."""
        if len(destination) != 32:
            raise ValueError("Expected a 32-character NomadPL destination hash")
        self.address = bytes.fromhex(destination)
        self.limits, self.identity = limits, identity
        self._link = None
        self._lock = threading.Lock()

    def _wait(self, predicate, deadline):
        """Wait for a path or link state until the shared deadline expires."""
        while not predicate():
            if time.monotonic() >= deadline:
                raise TimeoutError("Reticulum path or link timed out")
            time.sleep(0.05)

    def request(self, request):
        """Send one exchange over a verified link, serializing concurrent requests."""
        import RNS

        request.validate(self.limits)
        # Serialising avoids resource bursts on low-bandwidth links. Browser
        # connections queue here while the underlying encrypted link is reused.
        if not self._lock.acquire(timeout=self.limits.timeout):
            raise TimeoutError("NomadPL request queue timed out")
        try:
            deadline = time.monotonic() + self.limits.timeout
            if self._link is None or self._link.status != RNS.Link.ACTIVE:
                if not RNS.Transport.has_path(self.address):
                    RNS.Transport.request_path(self.address)
                self._wait(lambda: RNS.Transport.has_path(self.address), deadline)
                identity = RNS.Identity.recall(self.address)
                if identity is None:
                    raise ConnectionError("Destination identity unavailable")
                destination = RNS.Destination(
                    identity, RNS.Destination.OUT, RNS.Destination.SINGLE, *ASPECTS
                )
                if destination.hash != self.address:
                    raise ConnectionError(
                        "Selected destination is not a nomadpl.node destination"
                    )
                self._link = RNS.Link(destination)
                self._wait(
                    lambda: self._link.status in (RNS.Link.ACTIVE, RNS.Link.CLOSED),
                    deadline,
                )
                if self._link.status != RNS.Link.ACTIVE:
                    raise ConnectionError("Reticulum link closed")
                if self.identity is not None:
                    self._link.identify(self.identity)
            finished, result = threading.Event(), {}

            def received(receipt):
                """
                Capture the response and wake the thread waiting for this exchange.
                """
                result["response"] = receipt.response
                finished.set()

            def failed(receipt):
                """Mark the exchange as failed and release its waiting thread."""
                result["failed"] = True
                finished.set()

            remaining = max(0.01, deadline - time.monotonic())
            receipt = self._link.request(
                REQUEST_PATH,
                data=request.to_wire(),
                response_callback=received,
                failed_callback=failed,
                timeout=remaining,
                max_response_size=self.limits.response_body
                + self.limits.headers
                + 8192,
            )
            if receipt is False:
                raise ConnectionError("Reticulum request could not be sent")
            if not finished.wait(max(0, deadline - time.monotonic())):
                raise TimeoutError("NomadPL response timed out")
            if result.get("failed"):
                raise ConnectionError(
                    "NomadPL request failed or response exceeded limits"
                )
            try:
                return Response.from_wire(result["response"], self.limits)
            except (ValueError, KeyError, TypeError) as exc:
                raise ConnectionError(
                    "Peer did not return a valid NomadPL response"
                ) from exc
        except Exception:
            if self._link is not None:
                self._link.teardown()
                self._link = None
            raise
        finally:
            self._lock.release()

    def close(self):
        """Tear down the reusable link after any in-flight request finishes."""
        with self._lock:
            if self._link is not None:
                self._link.teardown()
                self._link = None
