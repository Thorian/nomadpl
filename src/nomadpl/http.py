import math
import re
from dataclasses import dataclass, field

from .urls import target_parts

TOKEN = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "proxy-connection",
}


@dataclass(frozen=True)
class Limits:
    request_body: int = 2 * 1024 * 1024
    response_body: int = 16 * 1024 * 1024
    url: int = 8192
    headers: int = 64 * 1024
    timeout: float = 120

    def __post_init__(self):
        """Reject nonpositive sizes and nonfinite timeouts at configuration time."""
        for value in (self.request_body, self.response_body, self.url, self.headers):
            if type(value) is not int or value <= 0:
                raise ValueError("Size limits must be positive integers")
        if not math.isfinite(self.timeout) or self.timeout <= 0:
            raise ValueError("Timeout must be finite and positive")


def checked_headers(headers, limit=65536):
    """Validate header pairs and enforce the total header-size limit."""
    if not isinstance(headers, (list, tuple)) or len(headers) > 200:
        raise ValueError("Invalid HTTP headers")
    result = []
    size = 0
    for item in headers:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            raise ValueError("Invalid header pair")
        key, value = item
        if (
            not isinstance(key, str)
            or not TOKEN.fullmatch(key)
            or not isinstance(value, str)
        ):
            raise ValueError("Invalid header")
        if any(
            ord(c) < 32 and c != "\t" or ord(c) == 127 or ord(c) > 255 for c in value
        ):
            raise ValueError("Invalid header value")
        size += len(key) + len(value) + 4
        result.append((key, value))
    if size > limit:
        raise ValueError("Headers too large")
    return result


def end_to_end(headers):
    """Remove hop-by-hop fields, including those named by Connection."""
    connection = {
        value.strip().lower()
        for key, values in headers
        if key.lower() == "connection"
        for value in values.split(",")
    }
    return [(k, v) for k, v in headers if k.lower() not in HOP_HEADERS | connection]


@dataclass
class Request:
    method: str
    target: str
    headers: list[tuple[str, str]] = field(default_factory=list)
    body: bytes = b""
    remote_identity: bytes | None = field(default=None, repr=False, compare=False)
    link_id: bytes | None = field(default=None, repr=False, compare=False)

    def validate(self, limits=Limits()):
        """Check the method, target, headers and body against transport limits."""
        if not isinstance(self.method, str) or not TOKEN.fullmatch(self.method):
            raise ValueError("Invalid method")
        target_parts(self.target)
        if len(self.target) > limits.url:
            raise ValueError("URL too long")
        self.headers = checked_headers(self.headers, limits.headers)
        if not isinstance(self.body, bytes) or len(self.body) > limits.request_body:
            raise ValueError("Request body too large")
        return self

    def header(self, name, default=None):
        """Return the first matching header using case-insensitive field names."""
        return next((v for k, v in self.headers if k.lower() == name.lower()), default)

    def to_wire(self):
        """Build the versioned request envelope without encoding binary content."""
        return {
            "version": 1,
            "method": self.method,
            "target": self.target,
            "headers": self.headers,
            "body": self.body,
        }

    @classmethod
    def from_wire(cls, data, limits=Limits()):
        """Read and validate a peer's request envelope before application use."""
        if not isinstance(data, dict) or data.get("version") != 1:
            raise ValueError("Unsupported NomadPL request")
        return cls(
            data["method"], data["target"], data["headers"], data["body"]
        ).validate(limits)


@dataclass
class Response:
    status: int = 200
    headers: list[tuple[str, str]] = field(default_factory=list)
    body: bytes = b""

    @classmethod
    def text(cls, status, text):
        """Create a UTF-8 plain-text response with the supplied status."""
        return cls(
            status, [("Content-Type", "text/plain; charset=utf-8")], text.encode()
        )

    def validate(self, limits=Limits()):
        """Check a final status, header list and bounded binary response body."""
        if type(self.status) is not int or not 200 <= self.status <= 599:
            raise ValueError("Invalid final HTTP status")
        self.headers = checked_headers(self.headers, limits.headers)
        if not isinstance(self.body, bytes) or len(self.body) > limits.response_body:
            raise ValueError("Response body too large")
        return self

    def to_wire(self):
        """Build the response envelope while preserving repeated headers."""
        return {
            "version": 1,
            "status": self.status,
            "headers": self.headers,
            "body": self.body,
        }

    @classmethod
    def from_wire(cls, data, limits=Limits()):
        """Read and validate a response received from a remote site."""
        if not isinstance(data, dict) or data.get("version") != 1:
            raise ValueError("Unsupported NomadPL response")
        return cls(data["status"], data["headers"], data["body"]).validate(limits)
