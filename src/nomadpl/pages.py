"""Static assets and explicitly configured executable pages."""

import logging
from pathlib import Path

from .access import permitted
from .external import execute_page
from .http import Limits, Request, Response
from .static import StaticSite
from .urls import decoded_path, target_parts

SOURCE_SUFFIXES = {
    ".php",
    ".phtml",
    ".phar",
    ".py",
    ".pl",
    ".rb",
    ".sh",
    ".cgi",
    ".inc",
    ".bak",
    ".old",
    ".swp",
}


class PageApplication:
    def __init__(
        self,
        root,
        *,
        interpreters=(),
        index=("index.html", "index.php"),
        require_identity=False,
        limits=Limits(),
        script_timeout=30,
        protocol="nomadpl",
    ):
        """Keep executable suffixes separate from files that may be served verbatim."""
        self.root = Path(root).resolve(strict=True)
        self.static = StaticSite(self.root, limits=limits)
        self.interpreters = {}
        for interpreter in interpreters:
            for suffix in interpreter.suffixes:
                suffix = suffix.lower()
                if suffix in self.interpreters:
                    raise ValueError("Two interpreters claim the same suffix")
                self.interpreters[suffix] = interpreter
        if not index or any(
            Path(name).name != name or name.startswith(".") for name in index
        ):
            raise ValueError("Index entries must be public filenames")
        self.index, self.require_identity, self.limits = index, require_identity, limits
        self.script_timeout, self.protocol = script_timeout, protocol

    def resolve(self, target):
        """Resolve one public path without admitting hidden, reserved or escaping paths."""
        path = decoded_path(target)
        parts = path.split("/")
        if any(
            part.startswith(".")
            or part.lower() == "state"
            or ":" in part
            or part.endswith((" ", "."))
            for part in parts
            if part
        ):
            raise PermissionError("Private path")
        file = (self.root / path.lstrip("/")).resolve()
        if not file.is_relative_to(self.root) or any(
            part.startswith(".") or part.lower() == "state"
            for part in file.relative_to(self.root).parts
        ):
            raise PermissionError("Path leaves public files")
        if file.name.lower().endswith(".allowed"):
            raise PermissionError("Private sidecar")
        return file

    def __call__(self, request, *, variables=None):
        """Serve assets or execute a page after checking its verified visitor identity."""
        request.validate(self.limits)
        if self.require_identity and request.remote_identity is None:
            return Response.text(
                403, "Identify your Reticulum link to access this site"
            )
        try:
            file = self.resolve(request.target)
            if file.is_dir():
                path, query = target_parts(request.target)
                if not path.endswith("/"):
                    return Response(
                        308, [("Location", path + "/" + ("?" + query if query else ""))]
                    )
                for name in self.index:
                    candidate = self.resolve(path + name)
                    if candidate.is_file():
                        file = candidate
                        request = Request(
                            request.method,
                            path + name + ("?" + query if query else ""),
                            request.headers,
                            request.body,
                            request.remote_identity,
                            request.link_id,
                        )
                        break
            if not file.is_file():
                return Response.text(404, "Not found")
            if not permitted(file, self.root, request.remote_identity):
                return Response.text(403, "Identity not allowed")
            interpreter = self.interpreters.get(file.suffix.lower())
            if interpreter is None:
                if file.suffix.lower() in SOURCE_SUFFIXES or file.name.lower() in (
                    "php.ini",
                    "composer.json",
                    "composer.lock",
                ):
                    return Response.text(403, "Script source is not public")
                if self.protocol == "nomadnet" and file.suffix.lower() != ".mu":
                    return Response.text(404, "Not a Micron page")
                return self.static(request)
            response = execute_page(
                interpreter,
                request,
                file,
                self.root,
                limits=self.limits,
                timeout=self.script_timeout,
                protocol=self.protocol,
                variables=variables,
            )
            if request.method == "HEAD":
                if not any(
                    key.lower() == "content-length" for key, _ in response.headers
                ):
                    response.headers.append(("Content-Length", str(len(response.body))))
                response.body = b""
            return response
        except PermissionError:
            return Response.text(403, "Forbidden")
        except TimeoutError:
            logging.warning("Page interpreter timed out")
            return Response.text(504, "Page interpreter timed out")
        except (ValueError, UnicodeError):
            logging.exception("Invalid page input or interpreter output")
            return Response.text(502, "Invalid page input or interpreter output")
        except OSError:
            logging.exception("Could not serve page")
            return Response.text(502, "Page interpreter or file unavailable")
