import io
import sys
from urllib.parse import unquote_to_bytes

from .http import Limits, Response, end_to_end
from .urls import target_parts


class WSGIApplication:
    """Run an ordinary WSGI application behind a NomadPL site."""

    def __init__(self, app, *, limits=Limits()):
        """Wrap a WSGI callable with the limits used by its hosting site."""
        self.app, self.limits = app, limits

    def __call__(self, request):
        """Build a WSGI environment and collect a bounded application response."""
        request.validate(self.limits)
        path, query = target_parts(request.target)
        host = request.header("Host", "nomadpl")
        environ = {
            "REQUEST_METHOD": request.method,
            "SCRIPT_NAME": "",
            "PATH_INFO": unquote_to_bytes(path).decode("latin-1"),
            "QUERY_STRING": query,
            "SERVER_NAME": host.split(":", 1)[0],
            "SERVER_PORT": "80",
            "SERVER_PROTOCOL": "HTTP/1.1",
            "REMOTE_ADDR": "",
            "wsgi.version": (1, 0),
            "wsgi.url_scheme": "http",
            "wsgi.input": io.BytesIO(request.body),
            "wsgi.errors": sys.stderr,
            "wsgi.multithread": True,
            "wsgi.multiprocess": False,
            "wsgi.run_once": False,
            "CONTENT_LENGTH": str(len(request.body)),
        }
        for key, value in end_to_end(request.headers):
            key = key.upper().replace("-", "_")
            if key == "CONTENT_TYPE":
                environ[key] = value
            elif key != "CONTENT_LENGTH":
                key = "HTTP_" + key
                environ[key] = environ[key] + "," + value if key in environ else value
        result, chunks, size = {}, [], 0
        body_started = False

        def write(chunk):
            """Append a binary WSGI chunk while enforcing the response-size limit."""
            nonlocal size, body_started
            if not result:
                raise RuntimeError("write called before start_response")
            if not isinstance(chunk, bytes):
                raise TypeError("WSGI response chunks must be bytes")
            if size + len(chunk) > self.limits.response_body:
                raise ValueError("WSGI response exceeds response limit")
            size += len(chunk)
            body_started = True
            chunks.append(chunk)

        def start_response(status, headers, exc_info=None):
            """
            Capture the final status and headers and expose the WSGI write callable.
            """
            try:
                if exc_info:
                    # WSGI commits headers at the first write or nonempty yield.
                    # Although the wire response is buffered, abort it rather than
                    # mixing earlier output into a replacement error response.
                    if body_started:
                        raise exc_info[1].with_traceback(exc_info[2])
                elif result:
                    raise RuntimeError("start_response called twice")
                result.update(status=int(status.split(" ", 1)[0]), headers=headers)
                return write
            finally:
                exc_info = None

        iterable = self.app(environ, start_response)
        try:
            for chunk in iterable:
                # Empty iterator chunks do not commit WSGI response headers.
                if chunk or not isinstance(chunk, bytes):
                    write(chunk)
        finally:
            if hasattr(iterable, "close"):
                iterable.close()
        response = Response(
            result["status"], end_to_end(result["headers"]), b"".join(chunks)
        ).validate(self.limits)
        if request.method == "HEAD":
            if not any(k.lower() == "content-length" for k, _ in response.headers):
                response.headers.append(("Content-Length", str(len(response.body))))
            response.body = b""
        return response
