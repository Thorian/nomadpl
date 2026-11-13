import re
from dataclasses import dataclass
from urllib.parse import unquote_to_bytes, urljoin, urlsplit, urlunsplit


def target_parts(target: str):
    """Validate origin-form request targets without normalising their query."""
    if (
        not isinstance(target, str)
        or not target.startswith("/")
        or target.startswith("//")
    ):
        raise ValueError(
            "Request target must be an absolute path beginning with one slash"
        )
    if (
        any(ord(c) < 33 or ord(c) > 126 for c in target)
        or "\\" in target
        or "#" in target
    ):
        raise ValueError(
            "URL must be ASCII percent-encoded, without fragments or backslashes"
        )
    if re.search(r"%(?![0-9a-fA-F]{2})", target):
        raise ValueError("Malformed percent escape")
    parts = urlsplit(target)
    if parts.scheme or parts.netloc:
        raise ValueError("Expected an origin-form URL")
    return parts.path, parts.query


def decoded_path(target: str) -> str:
    """Decode a UTF-8 URL path once and reject unsafe control characters."""
    path, _ = target_parts(target)
    decoded = unquote_to_bytes(path).decode("utf-8", errors="strict")
    if any(ord(c) < 32 or ord(c) == 127 for c in decoded) or "\\" in decoded:
        raise ValueError("Invalid path character")
    return decoded


@dataclass(frozen=True)
class URL:
    destination: str
    path: str = "/"
    query: str = ""
    fragment: str = ""

    @classmethod
    def parse(cls, value: str):
        """Parse a NomadPL address with a destination, path, query and fragment."""
        parts = urlsplit(value)
        if parts.scheme != "nomadpl" or not re.fullmatch(
            r"[0-9a-fA-F]{32}", parts.netloc
        ):
            raise ValueError("Use nomadpl://<32-character destination hash>/path")
        target_parts((parts.path or "/") + ("?" + parts.query if parts.query else ""))
        return cls(parts.netloc.lower(), parts.path or "/", parts.query, parts.fragment)

    @property
    def target(self):
        """Return the origin-form path and query, leaving the fragment client-side."""
        return self.path + ("?" + self.query if self.query else "")

    def resolve(self, reference: str):
        # Resolve using HTTP semantics; urllib does not classify custom schemes
        # as hierarchical by default. External HTTP links remain browser links.
        """Resolve a relative reference using ordinary hierarchical URL rules."""
        if reference.startswith("nomadpl:"):
            return self.parse(reference)
        base = urlunsplit(
            ("http", self.destination, self.path, self.query, self.fragment)
        )
        parts = urlsplit(urljoin(base, reference))
        if parts.scheme != "http" or not re.fullmatch(r"[0-9a-fA-F]{32}", parts.netloc):
            raise ValueError("Reference leaves the NomadPL network")
        return self.parse(
            urlunsplit(
                ("nomadpl", parts.netloc, parts.path, parts.query, parts.fragment)
            )
        )

    def __str__(self):
        """Serialize the address without discarding its query or fragment."""
        return urlunsplit(
            ("nomadpl", self.destination, self.path, self.query, self.fragment)
        )
