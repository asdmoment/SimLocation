import importlib.util
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


ROOT_DIR = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT_DIR / "bin" / "simlocation.py"
SPEC = importlib.util.spec_from_file_location("simlocation_under_test", MODULE_PATH)
simlocation = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(simlocation)


class SimLocationSmokeTests(unittest.TestCase):
    def test_module_exposes_core_cli_boundaries(self):
        self.assertTrue(callable(simlocation.clear_location))
        self.assertTrue(callable(simlocation.start_hold_session))
        self.assertTrue(callable(simlocation.parse_args))


class TunnelSelectionTests(unittest.TestCase):
    def test_auto_reuses_reachable_snapshot_without_starting_tunnel(self):
        with (
            patch.object(
                simlocation,
                "get_latest_rsd",
                return_value=("fd00::1", "1234"),
            ),
            patch.object(simlocation, "is_rsd_reachable", return_value=True),
            patch.object(simlocation, "request_fresh_rsd") as fresh,
        ):
            self.assertEqual(
                simlocation.acquire_rsd("udid", "auto"),
                ("fd00::1", "1234"),
            )
            fresh.assert_not_called()

    def test_auto_requests_fresh_tunnel_when_snapshot_is_unreachable(self):
        with (
            patch.object(
                simlocation,
                "get_latest_rsd",
                return_value=("fd00::1", "1234"),
            ),
            patch.object(
                simlocation,
                "is_rsd_reachable",
                side_effect=(False, True),
            ),
            patch.object(
                simlocation,
                "request_fresh_rsd",
                return_value=("fd00::2", "5678"),
            ) as fresh,
        ):
            self.assertEqual(
                simlocation.acquire_rsd("udid", "auto"),
                ("fd00::2", "5678"),
            )
            fresh.assert_called_once_with("udid", None)

    def test_rsd_mode_does_not_create_tunnel_when_snapshot_is_missing(self):
        with (
            patch.object(simlocation, "get_latest_rsd", return_value=None),
            patch.object(simlocation, "request_fresh_rsd") as fresh,
        ):
            self.assertIsNone(simlocation.acquire_rsd("udid", "rsd"))
            fresh.assert_not_called()

    def test_request_fresh_rsd_does_not_cancel_existing_tunnel(self):
        response = Mock()
        response.json.return_value = {"address": "fd00::2", "port": 5678}
        with patch.object(simlocation.requests, "get", return_value=response) as get:
            self.assertEqual(
                simlocation.request_fresh_rsd("udid"),
                ("fd00::2", "5678"),
            )

        requested_urls = [call.args[0] for call in get.call_args_list]
        self.assertNotIn(f"{simlocation.TUNNELD_URL}/cancel", requested_urls)


if __name__ == "__main__":
    unittest.main()
