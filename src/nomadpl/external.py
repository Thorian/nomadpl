"""Run configured page interpreters with CGI input and bounded output."""

import logging
import os
import re
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from .http import Limits, Response, end_to_end


@dataclass(frozen=True)
class Interpreter:
    command: tuple[str, ...]
    suffixes: tuple[str, ...]
    output: str = "cgi"

    def __post_init__(self):
        """Accept explicit argument lists and either CGI headers or plain output."""
        if (
            not isinstance(self.command, (tuple, list))
            or not self.command
            or any(
                not isinstance(arg, str) or not arg or "\0" in arg
                for arg in self.command
            )
        ):
            raise ValueError("Interpreter command must be a nonempty argument list")
        if (
            not isinstance(self.suffixes, (tuple, list))
            or not self.suffixes
            or any(
                not isinstance(suffix, str)
                or not suffix.startswith(".")
                or "/" in suffix
                or "\\" in suffix
                for suffix in self.suffixes
            )
        ):
            raise ValueError("Interpreter suffixes must be filename extensions")
        if self.output not in ("cgi", "body"):
            raise ValueError("Interpreter output must be cgi or body")


def stop_process(process):
    """Stop a timed-out interpreter and its process group where supported."""
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    elif process.poll() is None:
        subprocess.run(
            [
                str(Path(os.environ["SystemRoot"]) / "System32" / "taskkill.exe"),
                "/PID",
                str(process.pid),
                "/T",
                "/F",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    if process.poll() is None:
        process.kill()


def run_process(command, environment, body, *, cwd, timeout, output_limit):
    """Feed stdin while draining both pipes without buffering unlimited output."""
    deadline = time.monotonic() + timeout
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment,
        cwd=cwd,
        shell=False,
        start_new_session=os.name == "posix",
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    output, errors = bytearray(), bytearray()
    overflow = threading.Event()

    def drain(stream, target, limit):
        """Read a pipe in small chunks and stop its producer on overflow."""
        try:
            while chunk := stream.read(8192):
                if len(target) + len(chunk) > limit:
                    overflow.set()
                    stop_process(process)
                    break
                target.extend(chunk)
        finally:
            stream.close()

    def feed():
        """Write the request body without blocking the output readers."""
        try:
            process.stdin.write(body)
            process.stdin.flush()
        except (BrokenPipeError, OSError):
            pass
        finally:
            process.stdin.close()

    workers = [
        threading.Thread(
            target=drain, args=(process.stdout, output, output_limit), daemon=True
        ),
        threading.Thread(
            target=drain, args=(process.stderr, errors, 65536), daemon=True
        ),
        threading.Thread(target=feed, daemon=True),
    ]
    for worker in workers:
        worker.start()
    try:
        process.wait(timeout=max(0, deadline - time.monotonic()))
        for worker in workers:
            worker.join(timeout=max(0, deadline - time.monotonic()))
        if any(worker.is_alive() for worker in workers):
            raise TimeoutError("Interpreter pipes did not close")
        if overflow.is_set():
            raise ValueError("Interpreter output exceeds its limit")
        if process.returncode:
            logging.warning(
                "Page interpreter exited with status %s", process.returncode
            )
            raise ValueError("Page interpreter failed")
        return bytes(output)
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError("Page interpreter timed out") from exc
    finally:
        if process.poll() is None or any(worker.is_alive() for worker in workers):
            stop_process(process)
        process.wait(timeout=5)
        for worker in workers:
            worker.join(timeout=1)


def cgi_environment(request, script, root, *, protocol="nomadpl", variables=None):
    """Build a fresh CGI environment, keeping authentication outside HTTP headers."""
    # Do not copy credentials, PHP settings or other application environment state.
    environment = {
        key: os.environ[key]
        for key in ("PATH", "SystemRoot", "WINDIR", "TEMP", "TMP", "TMPDIR", "LANG")
        if key in os.environ
    }
    target = urlsplit(request.target)
    host = urlsplit("http://" + request.header("Host", "nomadpl.localhost"))
    environment.update(
        {
            "GATEWAY_INTERFACE": "CGI/1.1",
            "SERVER_SOFTWARE": "NomadPL",
            "SERVER_PROTOCOL": "HTTP/1.1",
            "SERVER_NAME": host.hostname or "nomadpl.localhost",
            "SERVER_PORT": str(host.port or 80),
            "REQUEST_METHOD": request.method,
            "REQUEST_URI": request.target,
            "QUERY_STRING": target.query,
            "SCRIPT_NAME": target.path,
            "SCRIPT_FILENAME": str(script),
            "DOCUMENT_ROOT": str(root),
            "CONTENT_LENGTH": str(len(request.body)),
            "CONTENT_TYPE": request.header("Content-Type", ""),
            "REMOTE_ADDR": "",
            "REDIRECT_STATUS": "200",
            "RNS_PROTOCOL": protocol,
        }
    )
    for key, value in end_to_end(request.headers):
        if key.lower() not in ("content-type", "content-length", "proxy"):
            name = "HTTP_" + key.upper().replace("-", "_")
            environment[name] = (
                environment[name] + "," + value if name in environment else value
            )
    identity = (
        request.remote_identity.hex() if request.remote_identity is not None else ""
    )
    environment.update(
        {
            "REMOTE_USER": identity,
            "AUTH_TYPE": "Reticulum" if identity else "",
            "RNS_REMOTE_IDENTITY": identity,
            "remote_identity": identity,
        }
    )
    if request.link_id is not None:
        environment["link_id"] = request.link_id.hex()
    if variables:
        environment.update(variables)
    if (
        any("\0" in value for value in environment.values())
        or sum(
            len(key.encode()) + len(value.encode()) + 2
            for key, value in environment.items()
        )
        > 65536
    ):
        raise ValueError("Page environment is too large or contains a null byte")
    return environment


def parse_output(output, interpreter, limits=Limits()):
    """Parse CGI status and repeated headers while keeping the body binary."""
    if interpreter.output == "body":
        return Response(
            200, [("Content-Type", "text/plain; charset=utf-8")], output
        ).validate(limits)
    separator = re.search(rb"\r?\n\r?\n", output)
    if separator is None:
        raise ValueError("CGI output has no header separator")
    head, body = output[: separator.start()], output[separator.end() :]
    if len(head) > limits.headers:
        raise ValueError("CGI headers exceed their limit")
    headers, status = [], None
    for line in head.decode("latin-1").replace("\r\n", "\n").split("\n"):
        key, separator, value = line.partition(":")
        if not separator:
            raise ValueError("Malformed CGI header")
        value = value.strip(" \t")
        if key.lower() == "status":
            if status is not None:
                raise ValueError("Repeated CGI status")
            status = int(value.split(" ", 1)[0])
        else:
            headers.append((key, value))
    if status is None:
        status = 302 if any(key.lower() == "location" for key, _ in headers) else 200
    return Response(status, end_to_end(headers), body).validate(limits)


def execute_page(
    interpreter,
    request,
    script,
    root,
    *,
    limits=Limits(),
    timeout=30,
    protocol="nomadpl",
    variables=None,
):
    """Execute only the configured command; request data never becomes an argument."""
    command = [
        argument.replace("{script}", str(script)) for argument in interpreter.command
    ]
    environment = cgi_environment(
        request, script, root, protocol=protocol, variables=variables
    )
    output = run_process(
        command,
        environment,
        request.body,
        cwd=root,
        timeout=timeout,
        output_limit=limits.response_body + limits.headers + 4,
    )
    return parse_output(output, interpreter, limits)
