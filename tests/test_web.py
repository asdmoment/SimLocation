"""Web API contracts. Device and tunnel operations are always mocked."""

import argparse
import functools
import http.client
import importlib.util
import json
import tempfile
import threading
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from test_simlocation import simlocation as cli

SPEC = importlib.util.spec_from_file_location("simlocation_web", cli.PROJECT_DIR / "bin/simlocation_web.py")
web = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(web)
PHONE = "00008130-000845CC01EA001C"
OTHER = "00008130-000845CC01EA001D"


class WebConsoleTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        directory = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.root = Path(directory)
        self.stack.enter_context(patch.object(cli, "RUNTIME_DIR", self.root))
        self.stack.enter_context(patch.object(cli, "read_devices", functools.partial(cli.read_devices, self.root / "devices.json")))
        self.stack.enter_context(patch.object(cli, "write_devices", functools.partial(cli.write_devices, devices_path=self.root / "devices.json")))
        args = argparse.Namespace(device=None, connection="auto", debug=False, log_file=str(self.root / "debug.log"))
        self.app = web.WebConsole(cli, "/unused/pmd3", args)
        self.server = web.ThreadingHTTPServer(("127.0.0.1", 0), web.ConsoleHandler)
        self.server.access_token = "test-token"
        self.server.app = self.app
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.app.close()
        self.stack.close()

    def request(self, path="/api/state", data=None, headers=None, raw=None):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=3)
        body = raw if raw is not None else json.dumps(data) if data is not None else None
        request_headers = {"X-SimLocation-Token": "test-token", "Content-Type": "application/json"}
        if headers:
            request_headers.update(headers)
        connection.request("POST" if body is not None else "GET", path, body, request_headers)
        response = connection.getresponse()
        content = response.read()
        json_body = content and response.getheader("Content-Type").startswith("application/json")
        result = json.loads(content) if json_body else content.decode()
        connection.close()
        return response.status, result

    def operation(self, action, **data):
        return self.request("/api/action", {"action": action, **data})

    def test_web_command_accepts_common_and_remote_options(self):
        args = cli.parse_args(["--device", "phone", "web", "--remote", "--port", "8765", "--debug"])
        self.assertEqual(args.device, "phone")
        self.assertTrue(args.debug)
        self.assertEqual(cli.map_bind_from_args(args), ("0.0.0.0", False))

    def test_token_required_for_static_and_api_even_on_loopback(self):
        for path in ("/", "/console.js", "/console.css", "/picker?embed=1", "/api/state", "/api/state?t=test-token"):
            self.assertEqual(self.request(path, headers={"X-SimLocation-Token": ""})[0], 403)
        status, html = self.request("/?t=test-token", headers={"X-SimLocation-Token": ""})
        self.assertEqual(status, 200)
        self.assertNotIn("{{", html)
        self.assertIn("/console.js?t=test-token", html)
        self.assertEqual(self.request("/?t=%E4%B8%AD%E6%96%87")[0], 403)
        self.assertEqual(self.request(headers={"Origin": "https://untrusted.example"})[0], 403)

    def test_only_known_assets_and_actions_are_exposed(self):
        for path in ("/../VERSION", "/var/devices.json", "/api/logs", "/console.html"):
            self.assertEqual(self.request(path)[0], 404)
        # The browser asks for this on every load without a token; answering
        # 403 would make a working console log an error against itself.
        self.assertEqual(self.request("/favicon.ico", headers={"X-SimLocation-Token": ""}), (204, ""))
        for data in ({"action": "shell"}, {"action": ["set"]}, [], None):
            status, _ = self.request("/api/action", raw=json.dumps(data))
            self.assertEqual(status, 400)
        self.assertEqual(self.request("/confirm", {"lat": 1, "lon": 2})[0], 404)

    def test_invalid_payloads_never_touch_device(self):
        with patch.object(cli, "auto_set_location", return_value=None) as apply:
            for data in ({"device": "../../bad", "lat": 1, "lon": 2},
                         {"device": PHONE, "lat": True, "lon": 2},
                         {"device": PHONE, "lat": 91, "lon": 2},
                         {"device": PHONE, "lat": 1, "lon": 2, "debug": "yes"},
                         {"device": PHONE, "lat": 1, "lon": 2, "connection": "bad"},
                         {"lat": 1, "lon": 2}):
                self.assertEqual(self.operation("set", **data)[0], 400)
            apply.assert_not_called()
        for speed in (0, -1, 1001, True, "5", float("nan")):
            self.assertEqual(self.operation("route", device=PHONE, points=[[1, 2], [2, 3]], speed=speed)[0], 400)
        self.assertEqual(self.operation("route", device=PHONE, points=[[1, 2]], speed=5)[0], 400)
        with patch.object(cli, "auto_set_route") as apply_route:
            for field in ("speed_noise", "position_noise"):
                for value in (-1, 101, 10**400, True, "5", None, float("nan"), float("inf")):
                    with self.subTest(field=field, value=value):
                        self.assertEqual(self.operation(
                            "route", device=PHONE, points=[[1, 2], [2, 3]], **{field: value}
                        )[0], 400)
            apply_route.assert_not_called()
        self.assertEqual(self.operation("clear_all")[0], 400)
        self.assertIsNone(self.app.job)

    def test_repeated_operations_use_cli_and_preserve_server(self):
        with patch.object(cli, "auto_set_location", return_value=None) as apply, patch.object(cli, "clear_location", return_value=None) as clear:
            self.assertEqual(self.operation("set", device=PHONE, lat=-33.8, lon=151.2, connection="rsd")[0], 202)
            self.app.close()
            apply.assert_called_once_with(-33.8, 151.2, "/unused/pmd3", connection_mode="rsd", log_path=None, udid=PHONE)
            self.assertEqual(self.request()[1]["job"]["status"], "succeeded")
            self.assertEqual(self.operation("clear", device=PHONE)[0], 202)
            self.app.close()
            clear.assert_called_once()
            self.assertEqual(self.request()[1]["job"]["id"], 2)

    def test_busy_operation_keeps_state_available_and_rejects_overlap(self):
        released = threading.Event()
        self.addCleanup(released.set)
        with patch.object(cli, "auto_set_location", side_effect=lambda *a, **k: released.wait(3)) as apply:
            self.operation("set", device=PHONE, lat=1, lon=2)
            self.assertEqual(self.request()[1]["job"]["status"], "running")
            self.assertEqual(self.operation("clear", device=PHONE)[0], 409)
            released.set()
            self.app.close()
            self.assertEqual(apply.call_count, 1)

    def test_failed_start_is_reported_and_can_be_retried(self):
        def failure(*_args, **_kwargs):
            cli.log_message("tunnel unavailable")
            raise SystemExit(1)
        with patch.object(cli, "auto_set_location", side_effect=failure):
            self.operation("set", device=PHONE, lat=1, lon=2)
            self.app.close()
        job = self.request()[1]["job"]
        self.assertEqual(job["status"], "failed")
        self.assertIn("tunnel unavailable", job["messages"])
        with patch.object(cli, "auto_set_location", return_value=None):
            self.assertEqual(self.operation("set", device=PHONE, lat=1, lon=2)[0], 202)
            self.app.close()
        self.assertEqual(self.app.job["status"], "succeeded")

    def test_route_import_is_validation_only_and_playback_keeps_options(self):
        for file_format, content in (("json", '{"points": [[1,2],[2,3]]}'),
                                     ("gpx", '<gpx><rte><rtept lat="1" lon="2"/><rtept lat="2" lon="3"/></rte></gpx>')):
            status, route = self.request("/api/route", {"format": file_format, "content": content})
            self.assertEqual(status, 200)
            self.assertEqual(route["points"], [[1, 2], [2, 3]])
            self.assertIsNone(self.app.job)
        with patch.object(cli, "auto_set_route", return_value=None) as apply:
            self.operation("route", device=PHONE, points=route["points"], speed=12, loop=True,
                           speed_noise=15, position_noise=3)
            self.app.close()
            args, kwargs = apply.call_args
            self.assertEqual(args[0].points, [(1, 2), (2, 3), (1, 2)])
            self.assertEqual(args[1:3], (12, True))
            self.assertEqual(kwargs["udid"], PHONE)
            self.assertEqual((kwargs["speed_noise"], kwargs["position_noise"]), (15, 3))
        self.assertFalse(list(self.root.glob("route-*")))

    def test_route_import_rejects_entities_oversize_and_bad_json(self):
        for content, file_format in (("garbage", "json"), ("[]", "json"),
                                     ('<!DOCTYPE gpx [<!ENTITY a "test">]><gpx>&a;</gpx>', "gpx"),
                                     (" " * (cli.MAX_ROUTE_BYTES + 1), "json")):
            self.assertEqual(self.request("/api/route", {"format": file_format, "content": content})[0], 400)
        self.assertEqual(self.request("/api/action", raw="{")[0], 400)
        self.assertEqual(self.request("/api/action", raw="{}", headers={"Content-Length": str(cli.MAX_ROUTE_BYTES * 2 + 8193)})[0], 413)

    def test_device_management_and_orphaned_session_visibility(self):
        self.operation("device_add", device=PHONE, alias="手机")
        self.app.close()
        self.assertEqual(cli.read_devices()["aliases"], {"手机": PHONE})
        self.operation("device_default", device="手机")
        self.app.close()
        cli.write_state(cli.state_path_for(OTHER), {"status": "ready", "pid": 99999999, "lat": 1, "lon": 2})
        state = self.request()[1]
        self.assertEqual(state["default"], PHONE)
        self.assertEqual(len(state["devices"]), 2)
        self.assertIn("stale", state["devices"][1]["description"])
        self.assertEqual(self.operation("device_add", device=OTHER, alias="手机")[0], 400)
        self.operation("device_remove", alias="手机")
        self.app.close()
        self.assertEqual(cli.read_devices()["aliases"], {})

    def test_clear_all_continues_after_one_device_fails(self):
        for udid in (PHONE, OTHER):
            cli.write_state(cli.state_path_for(udid), {"status": "ready"})
        with patch.object(cli, "clear_location", side_effect=[SystemExit(1), None]) as clear:
            self.operation("clear_all", confirm=True)
            self.app.close()
            self.assertEqual(clear.call_count, 2)
        self.assertEqual(self.app.job["status"], "failed")
        self.assertIn(PHONE, self.app.job["messages"][-1])

    def test_doctor_and_discovery_return_structured_results(self):
        checks = [{"label": "tunneld", "status": "error", "detail": "unavailable"}]
        with patch.object(cli, "collect_doctor_checks", return_value=checks) as doctor:
            self.operation("doctor", device=PHONE)
            self.app.close()
            doctor.assert_called_once_with("/unused/pmd3", device_flag=PHONE)
            self.assertEqual(self.app.job["result"]["checks"], checks)
        with patch.object(cli, "discover_devices", return_value=[PHONE]):
            self.operation("discover")
            self.app.close()
        state = self.request()[1]
        self.assertTrue(state["devices"][0]["discovered"])
        self.assertIsNotNone(state["scanned_at"])
