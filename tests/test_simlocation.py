import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch


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


class HoldSessionTests(unittest.TestCase):
    def test_default_start_timeout_is_sixty_seconds(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(simlocation.get_hold_start_timeout_seconds(), 60.0)

    def test_invalid_start_timeout_is_rejected(self):
        with patch.dict(
            os.environ,
            {"SIMLOCATION_START_TIMEOUT_SECONDS": "not-a-number"},
            clear=True,
        ):
            with self.assertRaisesRegex(ValueError, "SIMLOCATION_START_TIMEOUT_SECONDS"):
                simlocation.get_hold_start_timeout_seconds()

    def test_session_can_become_ready_after_old_twelve_second_limit(self):
        proc = Mock(pid=123)
        proc.poll.return_value = None
        with (
            patch.object(
                simlocation,
                "read_state",
                side_effect=(
                    {"status": "starting", "pid": 123},
                    {"status": "ready", "pid": 123},
                ),
            ),
            patch.object(simlocation.time, "monotonic", side_effect=(0, 13, 13)),
            patch.object(simlocation.time, "sleep"),
        ):
            self.assertTrue(
                simlocation.wait_for_hold_session(
                    proc,
                    Path("unused-state.json"),
                    timeout_seconds=60,
                )
            )

    def test_true_timeout_terminates_child_and_records_error(self):
        proc = Mock(pid=456)
        proc.poll.return_value = None
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            with (
                patch.object(simlocation.time, "monotonic", side_effect=(0, 61)),
                patch.object(simlocation, "terminate_child_process") as terminate,
            ):
                self.assertFalse(
                    simlocation.wait_for_hold_session(
                        proc,
                        state_path,
                        timeout_seconds=60,
                    )
                )

            terminate.assert_called_once_with(proc)
            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(state["status"], "error")
            self.assertEqual(state["pid"], 456)
            self.assertIn("60", state["error"])


class HeldSessionClearTests(unittest.IsolatedAsyncioTestCase):
    async def test_successful_clear_records_confirmation(self):
        simulation = Mock()
        simulation.clear = AsyncMock()
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            simlocation.write_state(
                state_path,
                {"status": "ready", "pid": 123, "lat": "1", "lon": "2"},
            )

            await simlocation.clear_held_location(simulation, state_path)

            state = simlocation.read_state(state_path)
            self.assertEqual(state["status"], "cleared")
            self.assertTrue(state["clear_confirmed"])
            self.assertIn("cleared_at", state)
            simulation.clear.assert_awaited_once()

    async def test_failed_clear_records_error_and_raises(self):
        simulation = Mock()
        simulation.clear = AsyncMock(side_effect=RuntimeError("clear failed"))
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            simlocation.write_state(state_path, {"status": "ready", "pid": 123})

            with self.assertRaisesRegex(RuntimeError, "clear failed"):
                await simlocation.clear_held_location(simulation, state_path)

            state = simlocation.read_state(state_path)
            self.assertEqual(state["status"], "error")
            self.assertFalse(state["clear_confirmed"])
            self.assertEqual(state["clear_error"], "clear failed")


class ClearLocationTests(unittest.TestCase):
    def test_confirmed_background_clear_does_not_acquire_new_tunnel(self):
        with (
            patch.object(simlocation, "resolve_device_udid", return_value="udid"),
            patch.object(simlocation, "stop_hold_session", return_value=True),
            patch.object(
                simlocation,
                "read_state",
                return_value={"status": "stopped", "clear_confirmed": True},
            ),
            patch.object(simlocation, "acquire_rsd") as acquire,
            patch.object(simlocation, "execute_dvt_location_action") as execute,
        ):
            simlocation.clear_location("pmd3")

        acquire.assert_not_called()
        execute.assert_not_called()

    def test_unconfirmed_background_clear_uses_compensating_dvt_clear(self):
        with (
            patch.object(simlocation, "resolve_device_udid", return_value="udid"),
            patch.object(simlocation, "stop_hold_session", return_value=True),
            patch.object(
                simlocation,
                "read_state",
                return_value={"status": "stopped", "clear_confirmed": False},
            ),
            patch.object(
                simlocation,
                "acquire_rsd",
                return_value=("fd00::1", "1234"),
            ) as acquire,
            patch.object(
                simlocation,
                "execute_dvt_location_action",
                return_value=True,
            ) as execute,
        ):
            simlocation.clear_location("pmd3")

        acquire.assert_called_once_with("udid", "auto", None)
        execute.assert_called_once_with(
            ("fd00::1", "1234"),
            "clear",
            log_path=None,
        )


class DoctorTests(unittest.TestCase):
    def test_parser_accepts_doctor_subcommand(self):
        with patch.object(sys, "argv", ["simlocation", "doctor"]):
            args = simlocation.parse_args()
        self.assertEqual(args.command, "doctor")

    def test_healthy_environment_has_no_errors_or_side_effects(self):
        snapshot = {
            "udid": [
                {
                    "tunnel-address": "fd00::1",
                    "tunnel-port": 1234,
                    "interface": "172.20.10.1",
                }
            ]
        }
        version_result = Mock(returncode=0, stdout="9.27.0\n", stderr="")
        with (
            patch.object(
                simlocation.importlib_metadata,
                "version",
                return_value="9.27.0",
            ),
            patch.object(simlocation.subprocess, "run", return_value=version_result),
            patch.object(simlocation, "get_tunneld_snapshot", return_value=snapshot),
            patch.object(
                simlocation,
                "read_devices",
                return_value={"default": "phone", "aliases": {"phone": "udid"}},
            ),
            patch.object(simlocation, "is_rsd_reachable", return_value=True),
            patch.object(
                simlocation,
                "read_state",
                return_value={"status": "ready", "pid": 123},
            ),
            patch.object(simlocation, "is_process_alive", return_value=True),
            patch.object(simlocation, "request_fresh_rsd") as fresh,
            patch.object(simlocation, "stop_hold_session") as stop,
            patch.object(simlocation, "execute_dvt_location_action") as dvt,
        ):
            checks = simlocation.collect_doctor_checks("pmd3")

        self.assertFalse([check for check in checks if check["status"] == "error"])
        self.assertEqual(simlocation.doctor_exit_code(checks), 0)
        fresh.assert_not_called()
        stop.assert_not_called()
        dvt.assert_not_called()

    def test_unreachable_tunneld_is_an_error(self):
        version_result = Mock(returncode=0, stdout="9.27.0\n", stderr="")
        with (
            patch.object(
                simlocation.importlib_metadata,
                "version",
                return_value="9.27.0",
            ),
            patch.object(simlocation.subprocess, "run", return_value=version_result),
            patch.object(
                simlocation,
                "get_tunneld_snapshot",
                side_effect=RuntimeError("connection refused"),
            ),
        ):
            checks = simlocation.collect_doctor_checks("pmd3")

        tunneld_check = next(check for check in checks if check["label"] == "tunneld")
        self.assertEqual(tunneld_check["status"], "error")
        self.assertEqual(simlocation.doctor_exit_code(checks), 1)

    def test_stale_ready_pid_is_a_warning(self):
        snapshot = {
            "udid": [
                {"tunnel-address": "fd00::1", "tunnel-port": 1234}
            ]
        }
        version_result = Mock(returncode=0, stdout="9.27.0\n", stderr="")
        with (
            patch.object(
                simlocation.importlib_metadata,
                "version",
                return_value="9.27.0",
            ),
            patch.object(simlocation.subprocess, "run", return_value=version_result),
            patch.object(simlocation, "get_tunneld_snapshot", return_value=snapshot),
            patch.object(
                simlocation,
                "read_devices",
                return_value={"default": "udid", "aliases": {}},
            ),
            patch.object(simlocation, "is_rsd_reachable", return_value=True),
            patch.object(
                simlocation,
                "read_state",
                return_value={"status": "ready", "pid": 123},
            ),
            patch.object(simlocation, "is_process_alive", return_value=False),
        ):
            checks = simlocation.collect_doctor_checks("pmd3")

        session_check = next(check for check in checks if check["label"] == "后台会话")
        self.assertEqual(session_check["status"], "warn")


if __name__ == "__main__":
    unittest.main()
