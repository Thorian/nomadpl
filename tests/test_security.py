"""Security regressions for bounded application admission."""

import unittest

from nomadpl.admission import RequestGate


class AdmissionTests(unittest.TestCase):
    def test_concurrency_release_and_rate_refill(self):
        """Reject excess work, release failed handlers, and refill only over time."""
        now = [0.0]
        gate = RequestGate(1, 1, 2, clock=lambda: now[0])
        with self.assertRaises(RuntimeError):
            with gate.admit() as accepted:
                self.assertTrue(accepted)
                with gate.admit() as excess:
                    self.assertFalse(excess)
                raise RuntimeError("application failed")
        with gate.admit() as accepted:
            self.assertTrue(accepted)
        with gate.admit() as accepted:
            self.assertFalse(accepted)
        now[0] = 1
        with gate.admit() as accepted:
            self.assertTrue(accepted)

    def test_transport_rejects_before_application_work(self):
        """An exhausted service gate never invokes its application or registry."""
        from unittest.mock import Mock

        from nomadpl.transport import Site

        service = object.__new__(Site)
        service._gate = RequestGate(1, 1, 1, clock=lambda: 0)
        service._handle_request = Mock()
        with service._gate.admit():
            response = service._respond(None, None, None, None, None, None)
        self.assertEqual(response["status"], 503)
        service._handle_request.assert_not_called()


class PrivateStateTests(unittest.TestCase):
    def test_private_state_is_not_served(self):
        """Block old default keys and encoded, case-varied paths without hiding public assets."""
        import tempfile
        from pathlib import Path

        from nomadpl import Request, StaticSite

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "state").mkdir()
            (root / "state" / "identity").write_bytes(b"dummy private key")
            (root / "index.html").write_text("Public page")
            site = StaticSite(root)
            for target in ("/state/identity", "/%73tate/identity", "/STATE/identity"):
                self.assertEqual(site(Request("GET", target)).status, 403)
            self.assertEqual(site(Request("GET", "/index.html")).status, 200)

    def test_cli_rejects_keys_in_any_site_root(self):
        """Validate all sites before loading any key, including cross-site exposure."""
        import argparse
        import tempfile
        from pathlib import Path
        from unittest.mock import patch

        from nomadpl import Limits
        from nomadpl.cli import _build_parser, _run_sites

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            config = root / "sites.toml"
            config.write_text(
                '[[sites]]\nroot="public"\nidentity="private/one"\n'
                '[[sites]]\nroot="other"\nidentity="public/two"\n'
            )
            args = argparse.Namespace(announce_interval=60, sites=str(config))
            with patch("nomadpl.cli.load_identity") as load:
                with self.assertRaisesRegex(ValueError, "outside every public"):
                    _run_sites(args, Limits(), _build_parser())
                load.assert_not_called()
            parser = _build_parser()
            self.assertEqual(parser.parse_args(["serve"]).root, "public")
