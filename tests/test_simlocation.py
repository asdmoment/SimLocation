"""Regression tests using fake device dependencies and isolated runtime files."""

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock


def load_cli():
    modules = {}
    imports = {
        "pymobiledevice3.remote.remote_service_discovery": "RemoteServiceDiscoveryService",
        "pymobiledevice3.services.dvt.instruments.location_simulation": "LocationSimulation",
        "pymobiledevice3.services.dvt.instruments.dvt_provider": "DvtProvider",
    }
    for name, attribute in imports.items():
        parts = name.split(".")
        for count in range(1, len(parts) + 1):
            module_name = ".".join(parts[:count])
            modules.setdefault(module_name, types.ModuleType(module_name))
        setattr(modules[name], attribute, mock.Mock(side_effect=AssertionError("Device I/O")))
    modules["requests"] = types.ModuleType("requests")
    modules["requests"].get = mock.Mock(side_effect=AssertionError("Network I/O"))
    path = Path(__file__).resolve().parents[1] / "bin" / "simlocation.py"
    spec = importlib.util.spec_from_file_location("simlocation", path)
    module = importlib.util.module_from_spec(spec)
    with mock.patch.dict(sys.modules, modules):
        spec.loader.exec_module(module)
    return module


cli = load_cli()


class CliTestCase(unittest.TestCase):
    def setUp(self):
        self.contexts = contextlib.ExitStack()
        self.addCleanup(self.contexts.close)


class ArgumentTests(CliTestCase):
    def setUp(self):
        super().setUp()
        self.contexts.enter_context(mock.patch.dict(os.environ, {}, clear=True))
        self.contexts.enter_context(contextlib.redirect_stderr(io.StringIO()))

    def test_device_before_after_and_between_coordinates(self):
        for argv in (
            ["--device", "phone", "set", "1", "2"],
            ["set", "--device", "phone", "1", "2"],
            ["set", "1", "-d", "phone", "2"],
            ["set", "1", "2", "--device=phone"],
            ["1", "2", "-d", "phone"],
        ):
            with self.subTest(argv=argv):
                args = cli.parse_args(argv)
                self.assertEqual((args.command, args.device, args.lat, args.lon),
                                 ("set", "phone", "1", "2"))

    def test_clear_and_map_keep_device_and_shared_options(self):
        for command in ("clear", "map"):
            for argv in (["-d", "phone", command], [command, "-d", "phone"]):
                with self.subTest(argv=argv):
                    args = cli.parse_args(argv + ["--connection", "rsd", "--debug"])
                    self.assertEqual((args.command, args.device, args.connection),
                                     (command, "phone", "rsd"))
                    self.assertTrue(args.debug)
        self.assertTrue(cli.parse_args(["clear", "--all"]).clear_all)
        self.assertTrue(cli.parse_args(["map", "--pick-only"]).pick_only)

    def test_legacy_coordinates_clear_and_environment_defaults(self):
        for argv in (["-1", "-2"], ["set", "-1", "-2"], ["--", "-1", "-2"]):
            with self.subTest(argv=argv):
                args = cli.parse_args(argv)
                self.assertEqual((args.lat, args.lon), ("-1", "-2"))
        self.assertEqual(cli.parse_args(["--clear", "-d", "phone"]).command, "clear")
        with mock.patch.dict(os.environ, {
            "SIMLOCATION_DEFAULT_LAT": "1", "SIMLOCATION_DEFAULT_LON": "2",
        }):
            args = cli.parse_args(["-d", "phone"])
            self.assertEqual((args.command, args.device, args.lat, args.lon),
                             ("set", "phone", "1", "2"))

    def test_worker_command_keeps_runtime_options(self):
        args = cli.parse_args([
            "--connection", "rsd", "--pid-file", "/unused/pid",
            "--state-file", "/unused/state", "--_hold-session", "set", "1", "2",
        ])
        self.assertTrue(args._hold_session)
        self.assertEqual((args.pid_file, args.state_file), ("/unused/pid", "/unused/state"))

    def test_negative_scientific_notation_and_explicit_separator(self):
        for argv in (
            ["-1e-3", "-2e-3"],
            ["set", "-1e-3", "-2e-3"],
            ["set", "--", "-1e-3", "-2e-3"],
        ):
            with self.subTest(argv=argv):
                args = cli.parse_args(argv)
                self.assertEqual((args.lat, args.lon), ("-1e-3", "-2e-3"))

    def test_invalid_input_fails_instead_of_falling_back(self):
        for argv in (
            ["set", "1", "2", "--speeed", "5"],
            ["clear", "--devic", "phone"],
            ["map", "--pick"],
            ["set", "1", "2", "3"],
            ["1", "2", "3"],
            ["set", "1"],
            ["sett", "1", "2"],
            ["set", "nan", "2"],
            ["set", "1", "inf"],
            ["set", "91", "2"],
            ["set", "1", "181"],
            ["set", "phone", "2"],
            ["set", "1", "2", "--clear"],
            ["--_hold-session", "clear"],
        ):
            with self.subTest(argv=argv), self.assertRaises(SystemExit) as raised:
                cli.parse_args(argv)
            self.assertEqual(raised.exception.code, 2)

    def test_nested_device_commands_and_help(self):
        args = cli.parse_args(["device", "add", "phone", "DEVICE-A", "--debug"])
        self.assertEqual((args.device_command, args.alias, args.udid),
                         ("add", "phone", "DEVICE-A"))
        self.assertTrue(args.debug)
        for argv in (["--help"], ["set", "--help"], ["device", "add", "--help"]):
            with self.subTest(argv=argv), contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    cli.parse_args(argv)
                self.assertEqual(raised.exception.code, 0)


class DeviceDiscoveryTests(CliTestCase):
    def test_usb_discovery_without_tunneld_on_current_cli(self):
        def run_usbmux(command, **kwargs):
            if "--no-color" in command:
                return subprocess.CompletedProcess(command, 2, "", "No such option: --no-color")
            return subprocess.CompletedProcess(command, 0, json.dumps([
                {"UniqueDeviceID": "DEVICE-A"}, {"UniqueDeviceID": "DEVICE-B"},
            ]), "")

        with mock.patch.object(cli, "get_tunneld_snapshot", side_effect=OSError("offline")), \
                mock.patch.object(cli.subprocess, "run", side_effect=run_usbmux):
            self.assertEqual(cli.discover_devices("/unused/pmd3"), ["DEVICE-A", "DEVICE-B"])

    def test_tunnel_and_usb_results_are_deduplicated(self):
        result = subprocess.CompletedProcess([], 0, '[{"UniqueDeviceID": "DEVICE-A"}]', "")
        with mock.patch.object(cli, "get_tunneld_snapshot", return_value={"DEVICE-A": [], "DEVICE-B": []}), \
                mock.patch.object(cli.subprocess, "run", return_value=result):
            self.assertEqual(cli.discover_devices("/unused/pmd3"), ["DEVICE-A", "DEVICE-B"])


class RsdTests(CliTestCase):
    def setUp(self):
        super().setUp()
        self.contexts.enter_context(mock.patch.object(cli, "log_message"))
        self.contexts.enter_context(mock.patch.object(cli.time, "sleep"))

    def test_requested_device_never_falls_back_to_another_tunnel(self):
        record = {"rsd_address": "::1", "rsd_port": 12345}
        for snapshot in ({"DEVICE-B": [record]}, [record], record, {"DEVICE-A": []}):
            with self.subTest(snapshot=snapshot):
                with mock.patch.object(cli, "get_tunneld_snapshot", return_value=snapshot) as fetch:
                    self.assertIsNone(cli.get_latest_rsd(udid="DEVICE-A"))
                    self.assertEqual(fetch.call_count, cli.RSD_FETCH_RETRIES)

    def test_requested_device_can_appear_on_retry(self):
        record = {"rsd_address": "::1", "rsd_port": 12345}
        with mock.patch.object(cli, "get_tunneld_snapshot", side_effect=[
            {"DEVICE-B": [record]}, {"DEVICE-A": [record]},
        ]):
            self.assertEqual(cli.get_latest_rsd(udid="DEVICE-A"), ("::1", "12345"))

    def test_no_target_retains_generic_discovery(self):
        snapshot = {"DEVICE-B": [{"rsd_address": "::1", "rsd_port": 12345}]}
        with mock.patch.object(cli, "get_tunneld_snapshot", return_value=snapshot):
            self.assertEqual(cli.get_latest_rsd(), ("::1", "12345"))

    def test_waits_for_transient_route_setup_on_the_same_tunnel(self):
        pair = ("::1", "12345")
        with mock.patch.object(cli, "is_rsd_reachable", side_effect=[False, True]) as probe:
            self.assertTrue(cli.wait_for_rsd_reachable(pair))
            self.assertEqual(probe.call_args_list, [mock.call("::1", "12345", None)] * 2)
        cli.time.sleep.assert_called_once_with(cli.RETRY_DELAY_SECONDS)

    def test_unreachable_tunnel_retries_are_bounded(self):
        with mock.patch.object(cli, "is_rsd_reachable", return_value=False) as probe:
            self.assertFalse(cli.wait_for_rsd_reachable(("::1", "12345")))
            self.assertEqual(probe.call_count, cli.RSD_CONNECT_RETRIES)
        self.assertEqual(cli.time.sleep.call_count, cli.RSD_CONNECT_RETRIES - 1)


class SessionTests(CliTestCase):
    def setUp(self):
        super().setUp()
        directory = self.contexts.enter_context(tempfile.TemporaryDirectory(prefix="simlocation-test-"))
        self.runtime = Path(directory)
        self.pid_path = self.runtime / "DEVICE-A.pid"
        self.state_path = self.runtime / "DEVICE-A.state.json"
        self.contexts.enter_context(mock.patch.object(cli, "RUNTIME_DIR", self.runtime))
        self.contexts.enter_context(mock.patch.object(cli, "log_message"))

    def start(self):
        return cli.start_hold_session("1", "2", "/unused/pmd3", "rsd", "DEVICE-A")

    def make_child(self, status="starting"):
        proc = mock.Mock(pid=12345, returncode=1)
        proc.poll.return_value = None
        self.pid_path.write_text(str(proc.pid))
        cli.write_state(self.state_path, {"pid": proc.pid, "status": status})
        return proc

    def test_no_session_and_stale_pid_are_safe_to_replace(self):
        self.assertTrue(cli.stop_hold_session(self.pid_path, self.state_path))
        self.pid_path.write_text("12345")
        with mock.patch.object(cli, "is_process_alive", return_value=False):
            self.assertTrue(cli.stop_hold_session(self.pid_path, self.state_path))
        self.assertFalse(self.pid_path.exists())

    def test_failed_stop_blocks_start_and_clear(self):
        with mock.patch.object(cli, "stop_hold_session", return_value=False), \
                mock.patch.object(cli.subprocess, "Popen") as spawn, \
                mock.patch.object(cli, "request_fresh_rsd") as refresh:
            self.assertFalse(self.start())
            with self.assertRaises(SystemExit) as raised:
                cli.clear_location("/unused/pmd3", udid="DEVICE-A")
            self.assertEqual(raised.exception.code, 1)
            spawn.assert_not_called()
            refresh.assert_not_called()

    def test_ready_session_keeps_child_running(self):
        proc = self.make_child("ready")
        with mock.patch.object(cli, "stop_hold_session", return_value=True), \
                mock.patch.object(cli.subprocess, "Popen", return_value=proc) as spawn:
            self.assertTrue(self.start())
        proc.terminate.assert_not_called()
        proc.wait.assert_not_called()
        self.assertEqual(spawn.call_args.kwargs["env"]["SIMLOCATION_UDID"], "DEVICE-A")

    def test_timeout_reaps_child_and_marks_failure(self):
        proc = self.make_child()
        with mock.patch.object(cli, "stop_hold_session", return_value=True), \
                mock.patch.object(cli.subprocess, "Popen", return_value=proc), \
                mock.patch.object(cli, "HOLD_START_TIMEOUT_SECONDS", 0):
            self.assertFalse(self.start())
        proc.terminate.assert_called_once_with()
        proc.wait.assert_called_once_with(timeout=cli.CMD_TIMEOUT_SECONDS)
        self.assertFalse(self.pid_path.exists())
        self.assertEqual(cli.read_state(self.state_path)["status"], "error")

    def test_reported_error_also_reaps_child(self):
        proc = self.make_child("error")
        state = {"pid": proc.pid, "status": "error", "error": "test connection failure"}
        cli.write_state(self.state_path, state)
        with mock.patch.object(cli, "stop_hold_session", return_value=True), \
                mock.patch.object(cli.subprocess, "Popen", return_value=proc):
            self.assertFalse(self.start())
        proc.terminate.assert_called_once_with()
        self.assertEqual(cli.read_state(self.state_path), state)

    def test_exited_child_cannot_be_reported_ready(self):
        proc = self.make_child("ready")
        proc.poll.return_value = 1
        with mock.patch.object(cli, "stop_hold_session", return_value=True), \
                mock.patch.object(cli.subprocess, "Popen", return_value=proc):
            self.assertFalse(self.start())
        proc.terminate.assert_not_called()
        proc.wait.assert_called_once()
        self.assertEqual(cli.read_state(self.state_path)["status"], "error")

    def test_cleanup_escalates_and_preserves_other_session_files(self):
        proc = self.make_child()
        proc.wait.side_effect = [subprocess.TimeoutExpired("child", 8), 0]
        self.pid_path.write_text("67890")
        state = {"pid": 67890, "status": "ready"}
        cli.write_state(self.state_path, state)
        cli.cleanup_failed_hold_session(proc, self.pid_path, self.state_path)
        proc.kill.assert_called_once_with()
        self.assertEqual(proc.wait.call_count, 2)
        self.assertEqual(cli.read_pid(self.pid_path), 67890)
        self.assertEqual(cli.read_state(self.state_path), state)

    @unittest.skipIf(sys.platform == "win32", "POSIX signal handling")
    def test_actual_child_is_reaped_after_startup_timeout(self):
        script = "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); print('ready', flush=True); time.sleep(60)"
        proc = subprocess.Popen([sys.executable, "-u", "-c", script], stdout=subprocess.PIPE)
        try:
            self.assertEqual(proc.stdout.readline().strip(), b"ready")
            self.pid_path.write_text(str(proc.pid))
            cli.write_state(self.state_path, {"pid": proc.pid, "status": "starting"})
            with mock.patch.object(cli, "stop_hold_session", return_value=True), \
                    mock.patch.object(cli.subprocess, "Popen", return_value=proc), \
                    mock.patch.object(cli, "HOLD_START_TIMEOUT_SECONDS", 0), \
                    mock.patch.object(cli, "CMD_TIMEOUT_SECONDS", 0.2):
                self.assertFalse(self.start())
            self.assertEqual(proc.returncode, -signal.SIGKILL)
            self.assertFalse(self.pid_path.exists())
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=5)
            proc.stdout.close()


if __name__ == "__main__":
    unittest.main()
