"""Small WSGI example: nomadpl serve --app echo_app:application."""

import json


def application(environ, start_response):
    """Echo request details as JSON, with a redirect route for browser testing."""
    if environ["PATH_INFO"] == "/redirect":
        start_response("303 See Other", [("Location", "/?redirected=1")])
        return [b""]
    length = int(environ.get("CONTENT_LENGTH", "0"))
    body = json.dumps(
        {
            "method": environ["REQUEST_METHOD"],
            "path": environ["PATH_INFO"],
            "query": environ["QUERY_STRING"],
            "body": environ["wsgi.input"].read(length).decode("utf-8", "replace"),
        }
    ).encode()
    start_response(
        "200 OK",
        [
            ("Content-Type", "application/json"),
            ("Set-Cookie", "visited=yes; Path=/; SameSite=Lax"),
            ("Content-Length", str(len(body))),
        ],
    )
    return [body]
