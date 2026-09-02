import asyncio
import contextlib
import http.client
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch


ROOT_DIR = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT_DIR / "bin" / "simlocation.py"
SPEC = importlib.util.spec_from_file_location("simlocation_under_test", MODULE_PATH)
simlocation = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(simlocation)

# The CLI prints operator messages through log_message(); keep test output readable.
_QUIET_STDOUT = contextlib.redirect_stdout(io.StringIO())


def setUpModule():
    _QUIET_STDOUT.__enter__()


def tearDownModule():
    _QUIET_STDOUT.__exit__(None, None, None)


def parse_cli(*argv):
    with contextlib.redirect_stderr(io.StringIO()):
        return simlocation.parse_args(list(argv))


def parse_cli_error(*argv):
    stderr = io.StringIO()
    with contextlib.redirect_stderr(stderr):
        with unittest.TestCase().assertRaises(SystemExit) as ctx:
            simlocation.parse_args(list(argv))
    return ctx.exception.code, stderr.getvalue()


class LauncherTests(unittest.TestCase):
    def test_selected_python_uses_sibling_pymobiledevice3_cli(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            bin_dir = Path(temp_dir)
            python_bin = bin_dir / "python"
            pmd3_bin = bin_dir / "pymobiledevice3"
            python_bin.write_text(
                "#!/bin/sh\nprintf '%s\\n' \"${SIMLOCATION_PMD3:-}\"\n",
                encoding="utf-8",
            )
            pmd3_bin.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            python_bin.chmod(0o755)
            pmd3_bin.chmod(0o755)
            env = os.environ.copy()
            env["SIMLOCATION_PYTHON"] = str(python_bin)
            env.pop("SIMLOCATION_PMD3", None)

            result = subprocess.run(
                [str(ROOT_DIR / "bin" / "simlocation"), "--help"],
                check=True,
                capture_output=True,
                text=True,
                env=env,
            )

            self.assertEqual(result.stdout.strip(), str(pmd3_bin))


class SimLocationSmokeTests(unittest.TestCase):
    def test_module_exposes_core_cli_boundaries(self):
        self.assertTrue(callable(simlocation.clear_location))
        self.assertTrue(callable(simlocation.start_hold_session))
        self.assertTrue(callable(simlocation.parse_args))


class CliParsingTests(unittest.TestCase):
    def test_device_flag_after_set_subcommand_is_honored(self):
        args = parse_cli("set", "--device", "phone", "34.2", "117.1")
        self.assertEqual(args.command, "set")
        self.assertEqual(args.device, "phone")
        self.assertEqual((args.lat, args.lon), ("34.2", "117.1"))

    def test_device_flag_after_clear_subcommand_is_honored(self):
        args = parse_cli("clear", "-d", "phone", "--all")
        self.assertEqual(args.command, "clear")
        self.assertEqual(args.device, "phone")
        self.assertTrue(args.clear_all)

    def test_top_level_flags_survive_subcommand_defaults(self):
        args = parse_cli("--debug", "-d", "phone", "--connection", "rsd", "set", "1", "2")
        self.assertTrue(args.debug)
        self.assertEqual(args.device, "phone")
        self.assertEqual(args.connection, "rsd")

    def test_extra_positional_after_subcommand_is_rejected(self):
        code, stderr = parse_cli_error("set", "34.2", "117.1", "junk")
        self.assertEqual(code, 2)
        self.assertIn("junk", stderr)

    def test_non_numeric_coordinates_are_rejected(self):
        code, stderr = parse_cli_error("set", "abc", "def")
        self.assertEqual(code, 2)
        self.assertIn("坐标必须是数字", stderr)

    def test_out_of_range_coordinates_are_rejected(self):
        code, stderr = parse_cli_error("set", "95", "10")
        self.assertEqual(code, 2)
        self.assertIn("纬度", stderr)
        code, stderr = parse_cli_error("set", "10", "181")
        self.assertEqual(code, 2)
        self.assertIn("经度", stderr)

    def test_legacy_positional_form_still_works(self):
        args = parse_cli("-d", "phone", "-33.8", "151.2")
        self.assertEqual(args.command, "set")
        self.assertEqual(args.device, "phone")
        self.assertEqual((args.lat, args.lon), ("-33.8", "151.2"))

    def test_legacy_clear_flag_still_works(self):
        args = parse_cli("--clear", "-d", "phone")
        self.assertEqual(args.command, "clear")
        self.assertEqual(args.device, "phone")

    def test_hold_session_child_invocation_parses(self):
        args = parse_cli(
            "--connection", "rsd", "--pid-file", "/tmp/p", "--state-file", "/tmp/s",
            "--_hold-session", "set", "34.2", "117.1",
        )
        self.assertTrue(args._hold_session)
        self.assertEqual(args.command, "set")
        self.assertEqual(args.pid_file, "/tmp/p")

    def test_version_flag_exits_cleanly(self):
        with contextlib.redirect_stdout(io.StringIO()):
            code, _ = parse_cli_error("--version")
        self.assertEqual(code, 0)


class DeviceResolutionTests(unittest.TestCase):
    def test_looks_like_udid_accepts_modern_and_legacy_shapes(self):
        self.assertTrue(simlocation.looks_like_udid("00008130-000845CC01EA001C"))
        self.assertTrue(simlocation.looks_like_udid("a" * 40))
        self.assertFalse(simlocation.looks_like_udid("myphone"))
        self.assertFalse(simlocation.looks_like_udid(""))
        self.assertFalse(simlocation.looks_like_udid(None))

    def test_unknown_alias_in_device_flag_fails_fast(self):
        with patch.object(
            simlocation,
            "read_devices",
            return_value={"default": None, "aliases": {"phone": "00008130-000845CC01EA001C"}},
        ):
            with self.assertRaises(SystemExit):
                simlocation.resolve_device_udid("pmd3", device_flag="typo")
            self.assertEqual(
                simlocation.resolve_device_udid("pmd3", device_flag="phone"),
                "00008130-000845CC01EA001C",
            )
            self.assertEqual(
                simlocation.resolve_device_udid("pmd3", device_flag="00008130-000845CC01EA001C"),
                "00008130-000845CC01EA001C",
            )

    def test_device_default_rejects_unknown_alias(self):
        with (
            patch.object(
                simlocation,
                "read_devices",
                return_value={"default": None, "aliases": {"phone": "00008130-000845CC01EA001C"}},
            ),
            patch.object(simlocation, "write_devices") as write,
        ):
            with self.assertRaises(SystemExit):
                simlocation.cmd_device_default("typo")
            write.assert_not_called()
            simlocation.cmd_device_default("phone")
            write.assert_called_once()

    def test_interactive_select_exits_on_eof(self):
        fake_stdin = Mock()
        fake_stdin.isatty.return_value = True
        with (
            patch.object(simlocation.sys, "stdin", fake_stdin),
            patch("builtins.input", side_effect=EOFError),
        ):
            with self.assertRaises(SystemExit):
                simlocation.interactive_device_select(
                    ["udid-a", "udid-b"], {"default": None, "aliases": {}}
                )

    def test_interactive_select_refuses_non_tty(self):
        fake_stdin = Mock()
        fake_stdin.isatty.return_value = False
        with patch.object(simlocation.sys, "stdin", fake_stdin):
            with self.assertRaises(SystemExit):
                simlocation.interactive_device_select(
                    ["udid-a", "udid-b"], {"default": None, "aliases": {}}
                )


class TunnelSelectionTests(unittest.TestCase):
    def test_auto_reuses_reachable_snapshot_without_starting_tunnel(self):
        with (
            patch.object(
                simlocation,
                "snapshot_rsd_candidates",
                return_value=[("fd00::1", "1234")],
            ),
            patch.object(simlocation, "is_rsd_reachable", return_value=True),
            patch.object(simlocation, "request_fresh_rsd") as fresh,
        ):
            self.assertEqual(
                simlocation.acquire_rsd("udid", "auto"),
                ("fd00::1", "1234"),
            )
            fresh.assert_not_called()

    def test_auto_tries_every_snapshot_candidate_before_starting_tunnel(self):
        with (
            patch.object(
                simlocation,
                "snapshot_rsd_candidates",
                return_value=[("fd00::1", "1234"), ("fd00::9", "4321")],
            ),
            patch.object(
                simlocation,
                "is_rsd_reachable",
                side_effect=(False, True),
            ) as reachable,
            patch.object(simlocation, "request_fresh_rsd") as fresh,
        ):
            self.assertEqual(
                simlocation.acquire_rsd("udid", "auto"),
                ("fd00::9", "4321"),
            )
            self.assertEqual(reachable.call_count, 2)
            fresh.assert_not_called()

    def test_auto_requests_fresh_tunnel_when_snapshot_is_unreachable(self):
        with (
            patch.object(
                simlocation,
                "snapshot_rsd_candidates",
                return_value=[("fd00::1", "1234")],
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

    def test_auto_does_not_start_tunnel_when_tunneld_is_unreachable(self):
        with (
            patch.object(simlocation, "snapshot_rsd_candidates", return_value=None),
            patch.object(simlocation, "request_fresh_rsd") as fresh,
        ):
            self.assertIsNone(simlocation.acquire_rsd("udid", "auto"))
            fresh.assert_not_called()

    def test_rsd_mode_does_not_create_tunnel_when_snapshot_is_missing(self):
        with (
            patch.object(simlocation, "snapshot_rsd_candidates", return_value=[]),
            patch.object(simlocation, "request_fresh_rsd") as fresh,
        ):
            self.assertIsNone(simlocation.acquire_rsd("udid", "rsd"))
            fresh.assert_not_called()

    def test_snapshot_never_falls_back_to_another_devices_tunnel(self):
        snapshot = {
            "other-udid": [{"tunnel-address": "fd00::1", "tunnel-port": 1234}],
        }
        with patch.object(simlocation, "get_tunneld_snapshot", return_value=snapshot):
            self.assertEqual(simlocation.snapshot_rsd_candidates("udid"), [])

    def test_snapshot_returns_every_tunnel_of_the_target_device(self):
        snapshot = {
            "udid": [
                {"tunnel-address": "fd00::1", "tunnel-port": 1234, "interface": "usb"},
                {"tunnel-address": "fd00::2", "tunnel-port": 5678, "interface": "wifi"},
                {"tunnel-address": "fd00::1", "tunnel-port": 1234},
            ],
            "other-udid": [{"tunnel-address": "fd00::9", "tunnel-port": 9}],
        }
        with patch.object(simlocation, "get_tunneld_snapshot", return_value=snapshot):
            self.assertEqual(
                simlocation.snapshot_rsd_candidates("udid"),
                [("fd00::1", "1234"), ("fd00::2", "5678")],
            )

    def test_snapshot_returns_none_when_tunneld_cannot_be_queried(self):
        with (
            patch.object(
                simlocation,
                "get_tunneld_snapshot",
                side_effect=RuntimeError("connection refused"),
            ) as snapshot,
            patch.object(simlocation.time, "sleep"),
        ):
            self.assertIsNone(simlocation.snapshot_rsd_candidates("udid"))
        self.assertEqual(snapshot.call_count, simlocation.RSD_FETCH_RETRIES)

    def test_request_fresh_rsd_issues_one_blocking_request_without_connection_type(self):
        response = Mock()
        response.json.return_value = {"address": "fd00::2", "port": 5678}
        with patch.object(simlocation.requests, "get", return_value=response) as get:
            self.assertEqual(
                simlocation.request_fresh_rsd("udid"),
                ("fd00::2", "5678"),
            )

        get.assert_called_once()
        self.assertEqual(get.call_args.args[0], f"{simlocation.TUNNELD_URL}/start-tunnel")
        self.assertEqual(get.call_args.kwargs["params"], {"udid": "udid"})
        self.assertEqual(
            get.call_args.kwargs["timeout"],
            simlocation.TUNNEL_START_TIMEOUT_SECONDS,
        )

    def test_request_fresh_rsd_timeout_rechecks_snapshot_instead_of_re_requesting(self):
        with (
            patch.object(
                simlocation.requests,
                "get",
                side_effect=simlocation.requests.Timeout("read timed out"),
            ) as get,
            patch.object(
                simlocation,
                "snapshot_rsd_candidates",
                return_value=[("fd00::3", "9")],
            ) as snapshot,
        ):
            self.assertEqual(simlocation.request_fresh_rsd("udid"), ("fd00::3", "9"))

        get.assert_called_once()
        snapshot.assert_called_once_with("udid", None, retries=1)

    def test_request_fresh_rsd_retries_bounded_on_http_error(self):
        failed = Mock(status_code=501, text='{"error": "task not created"}')
        error = simlocation.requests.HTTPError("501", response=failed)
        with (
            patch.object(simlocation.requests, "get", side_effect=error) as get,
            patch.object(simlocation.time, "sleep"),
        ):
            self.assertIsNone(simlocation.request_fresh_rsd("udid"))
        self.assertEqual(get.call_count, simlocation.TUNNEL_START_RETRIES)

    def test_tunnel_start_timeout_fits_inside_default_hold_start_timeout(self):
        self.assertLess(
            simlocation.TUNNEL_START_TIMEOUT_SECONDS,
            simlocation.HOLD_START_TIMEOUT_SECONDS,
        )


class HoldSessionStopTests(unittest.IsolatedAsyncioTestCase):
    async def test_stop_requested_before_ready_skips_setting_location(self):
        simulation = Mock()
        simulation.set = AsyncMock()
        simulation.clear = AsyncMock()

        def async_cm(value):
            cm = Mock()
            cm.__aenter__ = AsyncMock(return_value=value)
            cm.__aexit__ = AsyncMock(return_value=False)
            return Mock(return_value=cm)

        stop_event = asyncio.Event()
        stop_event.set()
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            with (
                patch.object(simlocation, "RemoteServiceDiscoveryService", async_cm(Mock())),
                patch.object(simlocation, "DvtSecureSocketProxyService", async_cm(Mock())),
                patch.object(simlocation, "LocationSimulation", async_cm(simulation)),
            ):
                await simlocation._hold_dvt_location_session(
                    ("fd00::1", "1234"), "1", "2", state_path, stop_event=stop_event
                )
            self.assertIsNone(simlocation.read_state(state_path))

        simulation.set.assert_not_awaited()
        simulation.clear.assert_not_awaited()


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


class SessionStateTests(unittest.TestCase):
    def test_ready_state_with_live_pid_is_ready(self):
        with patch.object(simlocation, "is_process_alive", return_value=True):
            self.assertEqual(
                simlocation.describe_session_state(
                    {"status": "ready", "pid": 123, "lat": "1", "lon": "2"}
                ),
                "ready (1, 2)",
            )

    def test_ready_state_with_dead_pid_is_reported_stale(self):
        with patch.object(simlocation, "is_process_alive", return_value=False):
            self.assertIn(
                "stale",
                simlocation.describe_session_state(
                    {"status": "ready", "pid": 123, "lat": "1", "lon": "2"}
                ),
            )

    def test_other_states(self):
        self.assertEqual(simlocation.describe_session_state(None), "—")
        self.assertEqual(simlocation.describe_session_state({"status": "starting"}), "starting")
        self.assertEqual(simlocation.describe_session_state({"status": "error"}), "error")
        self.assertEqual(simlocation.describe_session_state({"status": "stopped"}), "—")


@unittest.skipIf(sys.platform == "win32", "POSIX signal semantics")
class ProcessLivenessTests(unittest.TestCase):
    def test_permission_denied_means_process_exists(self):
        with patch.object(simlocation.os, "kill", side_effect=PermissionError):
            self.assertTrue(simlocation.is_process_alive(123))

    def test_missing_process_is_dead(self):
        with patch.object(simlocation.os, "kill", side_effect=ProcessLookupError):
            self.assertFalse(simlocation.is_process_alive(123))

    def test_invalid_pid_is_dead_without_signalling(self):
        with patch.object(simlocation.os, "kill") as kill:
            self.assertFalse(simlocation.is_process_alive(None))
            self.assertFalse(simlocation.is_process_alive(0))
            self.assertFalse(simlocation.is_process_alive("123"))
        kill.assert_not_called()


class PymobiledeviceResolutionTests(unittest.TestCase):
    def test_interpreter_sibling_wins_over_path(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            sibling = Path(temp_dir) / "pymobiledevice3"
            sibling.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            sibling.chmod(0o755)
            with (
                patch.dict(os.environ, {}, clear=True),
                patch.object(
                    simlocation,
                    "sibling_pymobiledevice3_candidates",
                    return_value=[sibling],
                ),
                patch.object(simlocation.shutil, "which", return_value="/usr/bin/pymobiledevice3"),
            ):
                self.assertEqual(simlocation.resolve_pymobiledevice3(), str(sibling))

    def test_path_is_used_when_no_sibling_exists(self):
        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(
                simlocation,
                "sibling_pymobiledevice3_candidates",
                return_value=[Path("/nonexistent/pymobiledevice3")],
            ),
            patch.object(simlocation.shutil, "which", return_value="/usr/bin/pymobiledevice3"),
        ):
            self.assertEqual(simlocation.resolve_pymobiledevice3(), "/usr/bin/pymobiledevice3")

    def test_optional_resolution_returns_none_instead_of_exiting(self):
        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(simlocation, "sibling_pymobiledevice3_candidates", return_value=[]),
            patch.object(simlocation.shutil, "which", return_value=None),
        ):
            self.assertIsNone(simlocation.resolve_pymobiledevice3(required=False))
            with self.assertRaises(SystemExit):
                simlocation.resolve_pymobiledevice3()


class MapServerTests(unittest.TestCase):
    def setUp(self):
        self.server = simlocation.HTTPServer(("127.0.0.1", 0), simlocation._MapRequestHandler)
        self.server.map_html = b"<html>map</html>"
        self.server.picked_coords = None
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            response = conn.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            conn.close()

    def test_get_serves_map_without_cors_headers(self):
        status, headers, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"<html>map</html>")
        self.assertNotIn("Access-Control-Allow-Origin", headers)

    def test_invalid_json_and_bad_coordinates_are_rejected(self):
        status, _, _ = self.request(
            "POST", "/confirm", body=b"not json", headers={"Content-Type": "application/json"}
        )
        self.assertEqual(status, 400)
        status, _, _ = self.request(
            "POST", "/confirm", body=b'{"lat": 95, "lon": 10}',
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(status, 400)
        status, _, _ = self.request("POST", "/confirm", body=b"", headers={"Content-Length": "0"})
        self.assertEqual(status, 400)
        self.assertIsNone(self.server.picked_coords)

    def test_oversized_body_is_rejected(self):
        body = b"{" + b" " * (simlocation.MAP_MAX_BODY_BYTES + 10) + b"}"
        status, _, _ = self.request(
            "POST", "/confirm", body=body, headers={"Content-Type": "application/json"}
        )
        self.assertEqual(status, 413)

    def test_valid_confirm_records_coordinates_and_stops_server(self):
        status, _, body = self.request(
            "POST", "/confirm", body=b'{"lat": "34.2", "lon": "117.1"}',
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"ok": True})
        self.assertEqual(self.server.picked_coords, (34.2, 117.1))
        self.thread.join(timeout=5)
        self.assertFalse(self.thread.is_alive())


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

    def test_missing_cli_is_reported_but_diagnosis_continues(self):
        with (
            patch.object(
                simlocation.importlib_metadata,
                "version",
                return_value="9.27.0",
            ),
            patch.object(simlocation.subprocess, "run") as run,
            patch.object(
                simlocation,
                "get_tunneld_snapshot",
                side_effect=RuntimeError("connection refused"),
            ),
        ):
            checks = simlocation.collect_doctor_checks(None)

        run.assert_not_called()
        labels = [check["label"] for check in checks]
        self.assertIn("pymobiledevice3 CLI", labels)
        self.assertIn("tunneld", labels)
        cli_check = next(check for check in checks if check["label"] == "pymobiledevice3 CLI")
        self.assertEqual(cli_check["status"], "error")

    def test_device_flag_selects_target_and_all_rsds_are_probed(self):
        snapshot = {
            "udid": [
                {"tunnel-address": "fd00::1", "tunnel-port": 1234},
                {"tunnel-address": "fd00::2", "tunnel-port": 5678},
            ],
            "other": [{"tunnel-address": "fd00::9", "tunnel-port": 9}],
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
                return_value={"default": "other", "aliases": {"phone": "udid"}},
            ),
            patch.object(
                simlocation,
                "is_rsd_reachable",
                side_effect=(False, True),
            ) as reachable,
            patch.object(simlocation, "read_state", return_value=None),
        ):
            checks = simlocation.collect_doctor_checks("pmd3", device_flag="phone")

        target_check = next(check for check in checks if check["label"] == "目标设备")
        self.assertIn("udid", target_check["detail"])
        self.assertIn("--device", target_check["detail"])
        rsd_check = next(check for check in checks if check["label"] == "RSD")
        self.assertEqual(rsd_check["status"], "ok")
        self.assertIn("fd00::2:5678", rsd_check["detail"])
        self.assertIn("不可达: fd00::1:1234", rsd_check["detail"])
        self.assertEqual(reachable.call_count, 2)

    def test_unknown_device_flag_is_an_error(self):
        version_result = Mock(returncode=0, stdout="9.27.0\n", stderr="")
        with (
            patch.object(
                simlocation.importlib_metadata,
                "version",
                return_value="9.27.0",
            ),
            patch.object(simlocation.subprocess, "run", return_value=version_result),
            patch.object(simlocation, "get_tunneld_snapshot", return_value={"udid": []}),
            patch.object(
                simlocation,
                "read_devices",
                return_value={"default": None, "aliases": {}},
            ),
        ):
            checks = simlocation.collect_doctor_checks("pmd3", device_flag="typo")

        target_check = next(check for check in checks if check["label"] == "目标设备")
        self.assertEqual(target_check["status"], "error")
        self.assertEqual(simlocation.doctor_exit_code(checks), 1)


if __name__ == "__main__":
    unittest.main()
