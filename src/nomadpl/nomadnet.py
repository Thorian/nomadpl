"""Serve Micron pages using NomadNet's existing page request format."""

import re
from urllib.parse import urlencode

from .admission import RequestGate
from .http import Request


def page_request(path, data, identity, link_id, limits):
    """Translate field_ values to POST and var_ values to GET without trusting identity input."""
    if not path.startswith("/page/"):
        raise ValueError("Not a page request")
    if data is None:
        data = {}
    if not isinstance(data, dict) or len(data) > 200:
        raise ValueError("Invalid NomadNet page data")
    fields, query, variables = [], [], {}
    for key, value in data.items():
        if (
            not isinstance(key, str)
            or not isinstance(value, str)
            or not re.fullmatch(r"(?:field|var)_[A-Za-z0-9_\[\]-]{1,128}", key)
            or "\0" in value
        ):
            raise ValueError("Invalid NomadNet field")
        variables[key] = value
        (fields if key.startswith("field_") else query).append(
            (key.split("_", 1)[1], value)
        )
    body = urlencode(fields).encode("ascii")
    target = path[len("/page") :] + ("?" + urlencode(query) if query else "")
    headers = [("Content-Type", "application/x-www-form-urlencoded")] if fields else []
    request = Request(
        "POST" if fields else "GET",
        target,
        headers,
        body,
        identity.hash if identity is not None else None,
        link_id,
    )
    return request.validate(limits), variables


class NomadNetSite:
    def __init__(
        self,
        identity,
        app,
        *,
        name="NomadPL host",
        max_requests=8,
        request_rate=8,
        request_burst=16,
    ):
        """Register existing Micron and interpreter page paths on a standard NomadNet destination."""
        import RNS

        self.app, self.name = app, name
        self._gate = RequestGate(max_requests, request_rate, request_burst)
        self.destination = RNS.Destination(
            identity, RNS.Destination.IN, RNS.Destination.SINGLE, "nomadnetwork", "node"
        )
        self.destination.set_max_request_size(
            app.limits.request_body + app.limits.headers + app.limits.url + 8192
        )
        for file in app.root.rglob("*"):
            if not file.is_file() or (
                file.suffix.lower() != ".mu"
                and file.suffix.lower() not in app.interpreters
            ):
                continue
            relative = file.relative_to(app.root).as_posix()
            try:
                app.resolve("/" + relative)
            except (ValueError, PermissionError):
                continue
            self.destination.register_request_handler(
                "/page/" + relative,
                response_generator=self._respond,
                allow=RNS.Destination.ALLOW_ALL,
            )

    @property
    def address(self):
        """Return the destination used by ordinary NomadNet browsers."""
        return self.destination.hash.hex()

    def announce(self):
        """Publish the configured display name in the NomadNet namespace."""
        self.destination.announce(app_data=self.name.encode("utf-8"))

    def _respond(self, path, data, request_id, link_id, remote_identity, requested_at):
        """Return Micron bytes and keep overload or interpreter errors readable in NomadNet."""
        with self._gate.admit() as accepted:
            if not accepted:
                return b">Site busy\nPlease try again later.\n"
            try:
                request, variables = page_request(
                    path, data, remote_identity, link_id, self.app.limits
                )
                response = self.app(request, variables=variables)
                if response.status != 200:
                    return (
                        f">Request failed ({response.status})\n".encode()
                        + response.body
                    )
                return response.body
            except (ValueError, TypeError, UnicodeError):
                return b">Invalid page request\n"
