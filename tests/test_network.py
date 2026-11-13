import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


@unittest.skipUnless(
    os.environ.get("NOMADPL_NETWORK_TEST") == "1",
    "Set NOMADPL_NETWORK_TEST=1 for real TCP/Reticulum test",
)
class NetworkTests(unittest.TestCase):
    def test_two_sites_and_http_gateway_over_real_reticulum(self):
        """
        Exercise independent static and dynamic sites through real encrypted links.
        """
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            for name in ("server", "client"):
                (root / name).mkdir()
            common = "[reticulum]\n  share_instance = No\n[interfaces]\n"
            (root / "server" / "config").write_text(
                common
                + f"  [[Test listener]]\n    type = TCPServerInterface\n    enabled = Yes\n    listen_ip = 127.0.0.1\n    listen_port = {port}\n"
            )
            (root / "client" / "config").write_text(
                common
                + f"  [[Test client]]\n    type = TCPClientInterface\n    enabled = Yes\n    target_host = 127.0.0.1\n    target_port = {port}\n"
            )
            web = root / "www"
            (web / "assets").mkdir(parents=True)
            (web / "index.html").write_bytes(b"<h1>Real Reticulum</h1>")
            (web / "assets" / "style.css").write_bytes(b"body { color: green; }")
            (web / "assets" / "image.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\xff")
            (web / "large.bin").write_bytes(bytes(range(256)) * 256)
            fixture = Path(__file__).with_name("network_peer.py")
            with (root / "server.log").open("w+") as log:
                server = subprocess.Popen(
                    [sys.executable, "-u", str(fixture), "server", str(root)],
                    stdout=log,
                    stderr=log,
                )
                try:
                    deadline = time.monotonic() + 20
                    while not (root / "ready.json").exists():
                        if server.poll() is not None or time.monotonic() >= deadline:
                            log.seek(0)
                            self.fail("Server failed to start: " + log.read())
                        time.sleep(0.1)
                    result = subprocess.run(
                        [sys.executable, "-u", str(fixture), "client", str(root)],
                        capture_output=True,
                        text=True,
                        timeout=90,
                    )
                    self.assertEqual(
                        result.returncode, 0, result.stdout + result.stderr
                    )
                    self.assertIn("NETWORK_SMOKE_OK", result.stdout)
                finally:
                    (root / "stop").touch()
                    try:
                        server.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        server.kill()
                        server.wait()
