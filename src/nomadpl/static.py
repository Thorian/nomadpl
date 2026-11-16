"""A bounded static HTTP application, independent of networking."""

import hashlib
import mimetypes
import os
import re
from datetime import timezone
from email.utils import formatdate, parsedate_to_datetime
from pathlib import Path

from .access import permitted
from .http import Limits, Response
from .urls import decoded_path, target_parts


class StaticSite:
    def __init__(
        self,
        root,
        *,
        limits=Limits(),
        index="index.html",
        cache_control="public, max-age=60",
    ):
        """Resolve the public root and configure index files and response limits."""
        self.root = Path(root).resolve(strict=True)
        if not self.root.is_dir():
            raise ValueError("Static root must be a directory")
        if Path(index).name != index:
            raise ValueError("Index must be a filename")
        self.limits, self.index, self.cache_control = limits, index, cache_control

    def __call__(self, request):
        """Serve a public file with conditional-request and byte-range support."""
        request.validate(self.limits)
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            return Response(405, [("Allow", "GET, HEAD, OPTIONS")])
        if request.method == "OPTIONS":
            return Response(204, [("Allow", "GET, HEAD, OPTIONS")])
        try:
            path = decoded_path(request.target)
        except (ValueError, UnicodeError):
            return Response.text(400, "Invalid URL path")
        segments = path.split("/")
        # Deny dot files, explicit traversal and Windows filesystem aliases.
        if any(
            part.startswith(".")
            or part.lower() == "state"
            or ":" in part
            or part.endswith((" ", "."))
            for part in segments
            if part
        ):
            return Response.text(403, "Forbidden")
        file = (self.root / path.lstrip("/")).resolve()
        if not file.is_relative_to(self.root):
            return Response.text(403, "Forbidden")
        if file.is_dir():
            raw, query = target_parts(request.target)
            if not raw.endswith("/"):
                return Response(
                    308, [("Location", raw + "/" + ("?" + query if query else ""))]
                )
            file = (file / self.index).resolve()
            if not file.is_relative_to(self.root):
                return Response.text(403, "Forbidden")
        if (
            any(
                part.startswith(".") or part.lower() == "state"
                for part in file.relative_to(self.root).parts
            )
            or file.name.lower().endswith(".allowed")
            or not permitted(file, self.root, request.remote_identity)
        ):
            return Response.text(403, "Forbidden")
        try:
            if not file.is_file():
                return Response.text(404, "Not found")
            with file.open("rb") as stream:
                stat = os.fstat(stream.fileno())
                if stat.st_size > self.limits.response_body:
                    return Response.text(413, "File exceeds response limit")
                body = stream.read(self.limits.response_body + 1)
            if len(body) > self.limits.response_body:
                return Response.text(413, "File exceeds response limit")
        except OSError:
            return Response.text(404, "Not found")
        etag = '"' + hashlib.sha256(body).hexdigest() + '"'
        mime = mimetypes.guess_type(file.name)[0] or "application/octet-stream"
        if mime.startswith("text/") or mime in (
            "application/javascript",
            "application/json",
        ):
            mime += "; charset=utf-8"
        headers = [
            ("Content-Type", mime),
            ("ETag", etag),
            ("Last-Modified", formatdate(stat.st_mtime, usegmt=True)),
            ("Cache-Control", self.cache_control),
            ("Accept-Ranges", "bytes"),
            ("X-Content-Type-Options", "nosniff"),
        ]
        match = request.header("If-None-Match")
        modified = request.header("If-Modified-Since")
        unchanged = False
        if match is not None:
            unchanged = any(
                item.strip().removeprefix("W/") in ("*", etag)
                for item in match.split(",")
            )
        elif modified:
            try:
                date = parsedate_to_datetime(modified)
                unchanged = (
                    int(stat.st_mtime)
                    <= date.replace(tzinfo=date.tzinfo or timezone.utc).timestamp()
                )
            except (ValueError, TypeError, OverflowError):
                pass
        if unchanged:
            return Response(304, headers)
        status = 200
        byte_range = request.header("Range") if request.method == "GET" else None
        if_range = request.header("If-Range")
        if byte_range and if_range and if_range != etag:
            try:
                if int(stat.st_mtime) > parsedate_to_datetime(if_range).timestamp():
                    byte_range = None
            except (ValueError, TypeError, OverflowError):
                byte_range = None
        if byte_range:
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", byte_range)
            # Unsupported/malformed/multiple ranges are ignored per HTTP semantics.
            if match and any(match.groups()):
                first, last = match.groups()
                start = int(first) if first else max(0, len(body) - int(last))
                end = min(int(last), len(body) - 1) if first and last else len(body) - 1
                if start > end or start >= len(body) or (not first and int(last) == 0):
                    return Response(
                        416, headers + [("Content-Range", f"bytes */{len(body)}")]
                    )
                headers.append(("Content-Range", f"bytes {start}-{end}/{len(body)}"))
                body, status = body[start : end + 1], 206
        headers.append(("Content-Length", str(len(body))))
        return Response(status, headers, b"" if request.method == "HEAD" else body)
