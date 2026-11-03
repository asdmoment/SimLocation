"""Web API contracts. Device and tunnel operations are always mocked."""

import argparse
import base64
import hashlib
import functools
import http.client
import importlib.util
import io
import json
import os
import re
import tempfile
import threading
import unittest
from contextlib import ExitStack, redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.parse import urlencode

from test_simlocation import simlocation as cli

SPEC = importlib.util.spec_from_file_location("simlocation_web", cli.PROJECT_DIR / "bin/simlocation_web.py")
web = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(web)
PHONE = "00008030-000000000000000A"
OTHER = "00008130-000000000000000C"
PASSWORD = "correct-horse-battery-staple"
PASSWORD_HASH = web.hash_password(PASSWORD)


class WebConsoleTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        directory = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.root = Path(directory)
        self.stack.enter_context(patch.object(cli, "RUNTIME_DIR", self.root))
        self.stack.enter_context(patch.object(cli, "read_devices", functools.partial(cli.read_devices, self.root / "devices.json")))
        self.stack.enter_context(patch.object(cli, "write_devices", functools.partial(cli.write_devices, devices_path=self.root / "devices.json")))
        # tunneld is never contacted; individual tests swap in a fake response.
        self.tunneld = self.stack.enter_context(patch.object(cli.requests, "get", side_effect=ConnectionError("no tunneld")))
        self.stack.enter_context(patch.dict(os.environ))
        for name in ("SIMLOCATION_DEFAULT_LAT", "SIMLOCATION_DEFAULT_LON", "SIMLOCATION_WEB_LABEL"):
            os.environ.pop(name, None)
        args = argparse.Namespace(device=None, connection="auto", debug=False, log_file=str(self.root / "debug.log"))
        self.app = web.WebConsole(cli, "/unused/pmd3", args)
        self.now = 1000.0
        self.server = web.ThreadingHTTPServer(("127.0.0.1", 0), web.ConsoleHandler)
        self.server.auth = web.Auth(PASSWORD_HASH, "tester", clock=lambda: self.now)
        self.server.allowed_hosts = set()
        self.server.app = self.app
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()
        self.cookie, self.csrf = self.login()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.app.close()
        self.stack.close()

    def raw(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        connection.request(method, path, body, headers or {})
        response = connection.getresponse()
        content = response.read()
        connection.close()
        return response, content

    def request(self, path="/api/state", data=None, headers=None, raw=None, auth=True):
        body = raw if raw is not None else json.dumps(data) if data is not None else None
        request_headers = {"Content-Type": "application/json"}
        if auth:
            request_headers.update({"Cookie": self.cookie, "X-SimLocation-CSRF": self.csrf})
        if headers:
            request_headers.update(headers)
        response, content = self.raw("POST" if body is not None else "GET", path, body, request_headers)
        json_body = content and (response.getheader("Content-Type") or "").startswith("application/json")
        return response.status, json.loads(content) if json_body else content.decode()

    def submit_login(self, username="tester", password=PASSWORD, headers=None):
        body = urlencode({"username": username, "password": password})
        return self.raw("POST", "/login", body, {"Content-Type": "application/x-www-form-urlencoded", **(headers or {})})

    def login(self, **kwargs):
        response, _ = self.submit_login(**kwargs)
        self.assertEqual(response.status, 303)
        cookie = response.getheader("Set-Cookie").split(";")[0]
        _response, page = self.raw("GET", "/", None, {"Cookie": cookie})
        return cookie, re.search(r'name="simloc-csrf" content="([^"]+)"', page.decode()).group(1)

    def operation(self, action, **data):
        return self.request("/api/action", {"action": action, **data})

    def upstream(self, status=200, content=b"\x89PNG tile", content_type="image/png"):
        return MagicMock(status_code=status, content=content, headers={"Content-Type": content_type})

    def test_picker_loads_nothing_from_other_origins(self):
        status, page = self.request("/picker?embed=1")
        self.assertEqual(status, 200)
        for host in ("unpkg.com", "tile.openstreetmap.org", "nominatim.openstreetmap.org/search"):
            self.assertNotIn(host, page)
        self.assertIn('src="/map/leaflet-1.9.4/leaflet.js"', page)
        self.assertIn('"/map/tile/{z}/{x}/{y}.png"', page)
        # The vendored files are byte-identical to what the SRI hashes pin.
        for name, content_type in (("leaflet.js", "text/javascript"), ("leaflet.css", "text/css")):
            response, body = self.raw("GET", f"/map/leaflet-1.9.4/{name}", None, {"Cookie": self.cookie})
            digest = base64.b64encode(hashlib.sha384(body).digest()).decode()
            self.assertIn(f'integrity="sha384-{digest}"', page, name)
            self.assertTrue(response.getheader("Content-Type").startswith(content_type))
        self.assertEqual(self.raw("GET", "/map/leaflet-1.9.4/images/marker-icon.png", None,
                                  {"Cookie": self.cookie})[0].status, 200)

    def test_map_routes_need_a_session_and_refuse_other_files(self):
        for path in ("/map/leaflet-1.9.4/leaflet.js", "/map/tile/1/0/0.png", "/map/search?q=x"):
            self.assertEqual(self.request(path, auth=False)[0], 401, path)
        for path in ("/map/leaflet-1.9.4/../console.html", "/map/leaflet-1.9.4/LICENSE",
                     "/map/tile/1/0/0.jpg", "/map/tile/1/0/0.png/x", "/map/other"):
            self.assertEqual(self.request(path)[0], 404, path)
        self.tunneld.assert_not_called()

    def test_tiles_are_validated_fetched_once_and_cached(self):
        self.tunneld.side_effect = None
        self.tunneld.return_value = self.upstream()
        for path in ("/map/tile/20/0/0.png", "/map/tile/2/4/0.png", "/map/tile/2/0/4.png"):
            self.assertEqual(self.request(path)[0], 400, path)
        self.tunneld.assert_not_called()
        response, body = self.raw("GET", "/map/tile/2/3/1.png", None, {"Cookie": self.cookie})
        self.assertEqual((response.status, body), (200, b"\x89PNG tile"))
        self.assertIn("max-age", response.getheader("Cache-Control"))
        self.assertEqual(self.raw("GET", "/map/tile/2/3/1.png", None, {"Cookie": self.cookie})[1], b"\x89PNG tile")
        self.tunneld.assert_called_once()
        url = self.tunneld.call_args.args[0]
        self.assertEqual(url, "https://tile.openstreetmap.org/2/3/1.png")
        self.assertTrue(self.tunneld.call_args.kwargs["headers"]["User-Agent"].startswith("SimLocation/"))

    def test_tile_cache_evicts_oldest_beyond_its_budget(self):
        self.tunneld.side_effect = None
        self.tunneld.return_value = self.upstream(content=b"x" * 400)
        with patch.object(web, "TILE_CACHE_BYTES", 1000):
            for x in range(3):
                self.app.map_proxy.tile(2, x, 0)
        self.assertEqual(list(self.app.map_proxy.tiles), [(2, 1, 0), (2, 2, 0)])
        self.assertEqual(self.app.map_proxy.tile_bytes, 800)

    def test_upstream_failures_become_502_and_are_not_cached(self):
        self.assertEqual(self.request("/map/tile/1/0/0.png")[0], 502)
        self.tunneld.side_effect = None
        self.tunneld.return_value = self.upstream(content=b"<html>", content_type="text/html")
        self.assertEqual(self.request("/map/tile/1/0/0.png")[0], 502)
        self.assertEqual(self.app.map_proxy.tiles, {})

    def test_search_is_forwarded_with_fixed_parameters_and_paced(self):
        self.tunneld.side_effect = None
        self.tunneld.return_value = self.upstream(content=json.dumps([{"display_name": "天安门"}]).encode(),
                                                  content_type="application/json")
        waits = []
        self.app.map_proxy.sleep = waits.append
        self.app.map_proxy.clock = lambda: 50.0
        status, body = self.request("/map/search?q=%E5%A4%A9%E5%AE%89%E9%97%A8&format=xml&limit=999",
                                    headers={"Accept-Language": "zh-CN,zh,en"})
        self.assertEqual((status, body), (200, [{"display_name": "天安门"}]))
        kwargs = self.tunneld.call_args.kwargs
        self.assertEqual(self.tunneld.call_args.args[0], "https://nominatim.openstreetmap.org/search")
        self.assertEqual(kwargs["params"], {"format": "json", "limit": "8", "dedupe": "0", "addressdetails": "1",
                                            "q": "天安门"})
        self.assertEqual(kwargs["headers"]["Accept-Language"], "zh-CN,zh,en")
        self.request("/map/search?q=again", headers={"Accept-Language": "<script>"})
        self.assertEqual(waits, [1.0])
        self.assertEqual(self.tunneld.call_args.kwargs["headers"]["Accept-Language"], "zh-CN,zh,en")
        self.assertEqual(self.request("/map/search?q=" + "a" * 201)[0], 400)
        self.assertEqual(self.request("/map/search")[0], 400)

    def test_web_command_accepts_common_and_remote_options(self):
        args = cli.parse_args(["--device", "phone", "web", "--remote", "--port", "8765", "--debug"])
        self.assertEqual(args.device, "phone")
        self.assertTrue(args.debug)
        self.assertEqual(cli.map_bind_from_args(args), ("0.0.0.0", False))
        self.assertTrue(cli.parse_args(["web", "--hash-password"]).hash_password)

    def test_everything_but_login_requires_a_session(self):
        response, _ = self.raw("GET", "/")
        self.assertEqual((response.status, response.getheader("Location")), (303, "/login"))
        # A URL token no longer grants anything.
        response, _ = self.raw("GET", "/?t=" + self.csrf)
        self.assertEqual(response.status, 303)
        for path in ("/console.js", "/picker?embed=1", "/api/state", "/api/favorites"):
            self.assertEqual(self.request(path, auth=False)[0], 401, path)
        self.assertEqual(self.request("/api/action", {"action": "discover"}, auth=False)[0], 401)
        self.assertEqual(self.request("/console.css", auth=False)[0], 200)
        status, page = self.request("/login", auth=False)
        self.assertEqual(status, 200)
        self.assertIn('autocomplete="current-password"', page)
        self.assertNotIn("{{", page)

    def test_login_cookie_is_httponly_strict_and_api_needs_csrf(self):
        response, _ = self.submit_login()
        cookie = response.getheader("Set-Cookie")
        for attribute in ("HttpOnly", "SameSite=Strict", "Path=/"):
            self.assertIn(attribute, cookie)
        self.assertNotIn("Secure", cookie)
        self.assertEqual(self.request()[0], 200)
        self.assertEqual(self.request(headers={"X-SimLocation-CSRF": ""})[0], 401)
        self.assertEqual(self.request(headers={"X-SimLocation-CSRF": "x" + self.csrf})[0], 401)
        self.assertEqual(self.request(headers={"Origin": "http://evil.example"})[0], 403)
        self.assertEqual(self.request(headers={"Sec-Fetch-Site": "cross-site"})[0], 403)
        self.assertEqual(self.request(headers={"Sec-Fetch-Site": "same-origin"})[0], 200)
        response, page = self.raw("GET", "/", None, {"Cookie": self.cookie})
        self.assertIn("frame-ancestors 'self'", response.getheader("Content-Security-Policy"))
        self.assertNotIn(b"{{", page)
        self.assertNotIn(b"?t=", page)
        # Each login is a fresh session; a stale page's CSRF token stops working.
        other_cookie, other_csrf = self.login()
        self.assertNotEqual((other_cookie, other_csrf), (self.cookie, self.csrf))
        self.assertEqual(self.request(headers={"Cookie": other_cookie})[0], 401)

    def test_wrong_credentials_are_throttled_per_client(self):
        for _ in range(web.LOGIN_FAILURES_PER_CLIENT):
            response, page = self.submit_login(password="wrong")
            self.assertEqual(response.status, 401)
            self.assertIsNone(response.getheader("Set-Cookie"))
        response, page = self.submit_login()
        self.assertEqual(response.status, 429)
        self.assertIn("分钟后再试", page.decode())
        self.assertTrue(int(response.getheader("Retry-After")) > 0)
        self.now += web.LOGIN_WINDOW_SECONDS + 1
        self.assertEqual(self.submit_login(username="someone", password=PASSWORD)[0].status, 401)
        self.assertEqual(self.submit_login()[0].status, 303)
        for body, content_type in (("password=" + "x" * 5000, "application/x-www-form-urlencoded"),
                                   (json.dumps({"password": PASSWORD}), "application/json")):
            response, _ = self.raw("POST", "/login", body, {"Content-Type": content_type})
            self.assertEqual(response.status, 400)
        self.assertEqual(self.submit_login(headers={"Origin": "http://evil.example"})[0].status, 403)
        # A same-origin form POST under no-referrer arrives as "Origin: null";
        # only the unforgeable Sec-Fetch-Site tells it apart from a sandboxed frame.
        self.assertEqual(self.submit_login(headers={"Origin": "null"})[0].status, 403)
        self.assertEqual(self.submit_login(headers={"Origin": "null", "Sec-Fetch-Site": "cross-site"})[0].status, 403)
        self.assertEqual(self.submit_login(headers={"Origin": "null", "Sec-Fetch-Site": "same-origin"})[0].status, 303)
        response, _ = self.raw("GET", "/login")
        self.assertEqual(response.getheader("Referrer-Policy"), "same-origin")

    def test_shared_failure_ceiling_spares_addresses_that_logged_in_before(self):
        auth = web.Auth(PASSWORD_HASH, "tester", clock=lambda: self.now)
        with patch.object(web, "verify_password", side_effect=lambda password, _parsed: password == PASSWORD):
            self.assertIsNotNone(auth.login("10.77.77.2", "tester", PASSWORD))
            for index in range(web.LOGIN_FAILURES_TOTAL):
                self.assertIsNone(auth.login(f"10.0.0.{index}", "tester", "wrong"))
            # Unknown addresses stop at the shared ceiling, the right password included...
            with self.assertRaises(web.LoginThrottled):
                auth.login("10.9.9.9", "tester", PASSWORD)
            # ...but guessing from elsewhere cannot lock out an address that logged in before.
            self.assertIsNotNone(auth.login("10.77.77.2", "tester", PASSWORD))
            for _ in range(web.LOGIN_FAILURES_PER_CLIENT):
                self.assertIsNone(auth.login("10.77.77.2", "tester", "wrong"))
            with self.assertRaises(web.LoginThrottled):
                auth.login("10.77.77.2", "tester", PASSWORD)
            self.now += web.LOGIN_WINDOW_SECONDS + 1
            self.assertIsNotNone(auth.login("10.9.9.9", "tester", PASSWORD))
            self.now += web.TRUSTED_CLIENT_SECONDS + 1
            for index in range(web.LOGIN_FAILURES_TOTAL):
                auth.login(f"10.0.1.{index}", "tester", "wrong")
            with self.assertRaises(web.LoginThrottled):
                auth.login("10.77.77.2", "tester", PASSWORD)

    def test_sessions_expire_and_logout_revokes(self):
        self.now += web.SESSION_IDLE_SECONDS - 10
        self.assertEqual(self.request()[0], 200)
        self.now += web.SESSION_IDLE_SECONDS - 10
        self.assertEqual(self.request()[0], 200, "activity extends the idle window")
        self.now += web.SESSION_IDLE_SECONDS + 1
        self.assertEqual(self.request()[0], 401)
        self.cookie, self.csrf = self.login()
        day = 24 * 3600
        for _ in range(web.SESSION_MAX_SECONDS // (6 * day)):
            self.now += 6 * day
            self.assertEqual(self.request()[0], 200)
        self.now += day + 1
        self.assertEqual(self.request()[0], 401, "absolute lifetime is not extended by activity")
        self.cookie, self.csrf = self.login()
        self.assertEqual(self.request("/logout", {}, auth=False)[0], 401)
        status, _ = self.request("/logout", {})
        self.assertEqual(status, 200)
        self.assertEqual(self.request()[0], 401)
        response, _ = self.raw("GET", "/login", None, {"Cookie": self.cookie})
        self.assertEqual(response.status, 200)

    def test_rebinding_host_names_are_refused(self):
        for host in ("evil.example", "evil.example:8765", "10.77.77.1.nip.io", "[::1"):
            self.assertEqual(self.request(headers={"Host": host})[0], 421, host)
            self.assertEqual(self.submit_login(headers={"Host": host})[0].status, 421)
        for host in ("localhost:1", "127.0.0.1", "10.77.77.1:8765", "[::1]:8765"):
            self.assertEqual(self.request(headers={"Host": host})[0], 200, host)
        self.server.allowed_hosts = {"mac.local"}
        self.assertEqual(self.request(headers={"Host": "mac.local:8765"})[0], 200)

    def test_one_time_link_without_password(self):
        self.server.auth = web.Auth(None, clock=lambda: self.now)
        code = self.server.auth.issue_code()
        self.assertEqual(self.submit_login()[0].status, 400)
        response, _ = self.raw("GET", "/login?" + urlencode({"code": "guess"}))
        self.assertEqual(response.status, 401)
        response, _ = self.raw("GET", "/login?" + urlencode({"code": code}))
        self.assertEqual(response.status, 303)
        self.assertIn("HttpOnly", response.getheader("Set-Cookie"))
        response, page = self.raw("GET", "/login?" + urlencode({"code": code}))
        self.assertEqual(response.status, 401)
        self.assertIn("一次性", page.decode())
        expired = self.server.auth.issue_code()
        self.now += web.LOGIN_CODE_SECONDS + 1
        self.assertIsNone(self.server.auth.redeem_code(expired))

    def test_password_hash_format_and_validation(self):
        self.assertNotIn("$", PASSWORD_HASH)
        self.assertEqual(PASSWORD_HASH.split(":")[:4], ["scrypt", "16384", "8", "1"])
        parsed = web.parse_password_hash(PASSWORD_HASH)
        self.assertTrue(web.verify_password(PASSWORD, parsed))
        self.assertFalse(web.verify_password(PASSWORD + "x", parsed))
        self.assertNotEqual(web.hash_password(PASSWORD), PASSWORD_HASH, "salt must be random")
        scheme, n, r, p, salt, key = PASSWORD_HASH.split(":")
        for bad in ("", "plain-password", ":".join(["bcrypt", n, r, p, salt, key]),
                    ":".join([scheme, str(2 ** 20), r, p, salt, key]), ":".join([scheme, n, "1", p, salt, key]),
                    ":".join([scheme, n, r, p, "c2FsdA", key]), ":".join([scheme, "x", r, p, salt, key])):
            with self.assertRaises(ValueError, msg=bad):
                web.parse_password_hash(bad)
        with self.assertRaises(ValueError):
            web.Auth("not-a-hash")

    def test_hash_password_command_reads_a_piped_password(self):
        output = io.StringIO()
        with redirect_stdout(output):
            web.print_password_hash(cli, io.StringIO(PASSWORD + "\n"))
        line = output.getvalue().strip()
        self.assertTrue(line.startswith("SIMLOCATION_WEB_PASSWORD_HASH=scrypt:"))
        self.assertTrue(web.verify_password(PASSWORD, web.parse_password_hash(line.split("=", 1)[1])))
        with redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
            web.print_password_hash(cli, io.StringIO("short\n"))

    def test_only_known_assets_and_actions_are_exposed(self):
        for path in ("/../VERSION", "/var/devices.json", "/api/logs", "/console.html", "/login.html"):
            self.assertEqual(self.request(path)[0], 404, path)
        # The browser asks for this on every load without credentials; answering
        # 401 would make a working console log an error against itself.
        self.assertEqual(self.request("/favicon.ico", auth=False), (204, ""))
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

    def test_favorites_are_validated_persisted_and_never_touch_device(self):
        with patch.object(cli, "auto_set_location") as apply:
            status, result = self.request("/api/favorites", {"op": "add", "name": "  图书馆\n东门 ", "lat": 1.5, "lon": 2.25})
            self.assertEqual(status, 200)
            self.assertEqual(result["items"][0] | {"id": None}, {"id": None, "name": "图书馆 东门", "lat": 1.5, "lon": 2.25})
            self.request("/api/favorites", {"op": "add", "name": "公园", "lat": -3, "lon": 4})
            status, result = self.request("/api/favorites")
            self.assertEqual([item["name"] for item in result["items"]], ["公园", "图书馆 东门"])
            stored = json.loads((self.root / "favorites.json").read_text(encoding="utf-8"))
            self.assertEqual(len(stored["items"]), 2)
            for bad in ({"op": "add", "name": "", "lat": 1, "lon": 2}, {"op": "add", "name": "x" * 61, "lat": 1, "lon": 2},
                        {"op": "add", "name": "a\x01b", "lat": 1, "lon": 2}, {"op": "add", "name": "ok", "lat": 91, "lon": 2},
                        {"op": "add", "name": "ok", "lat": True, "lon": 2}, {"op": "add", "name": "dup", "lat": 1.5000001, "lon": 2.25},
                        {"op": "remove", "id": "000000000000"}, {"op": "rename"}, {"op": ["add"]}):
                status, error = self.request("/api/favorites", bad)
                self.assertEqual(status, 400, bad)
            self.assertIn("图书馆 东门", self.request("/api/favorites", {"op": "add", "name": "dup", "lat": 1.5, "lon": 2.25})[1]["error"])
            remove = result["items"][0]["id"]
            status, result = self.request("/api/favorites", {"op": "remove", "id": remove})
            self.assertEqual([item["name"] for item in result["items"]], ["图书馆 东门"])
            apply.assert_not_called()
        self.assertIsNone(self.app.job)
        self.assertEqual(self.request("/api/favorites", {"op": "add", "name": "x", "lat": 1, "lon": 2}, auth=False)[0], 401)

    def test_damaged_favorites_file_keeps_valid_rows(self):
        (self.root / "favorites.json").write_text(json.dumps({"items": [
            {"id": "0123456789ab", "name": "好", "lat": 1, "lon": 2}, {"id": "../etc", "name": "坏", "lat": 1, "lon": 2},
            {"id": "0123456789ac", "name": "", "lat": 1, "lon": 2}, {"id": "0123456789ad", "name": "远", "lat": 100, "lon": 2},
            "junk"]}), encoding="utf-8")
        self.assertEqual([item["name"] for item in self.request("/api/favorites")[1]["items"]], ["好"])
        (self.root / "favorites.json").write_text("{", encoding="utf-8")
        self.assertEqual(self.request("/api/favorites")[1]["items"], [])
        with patch.object(web, "FAVORITES_MAX", 1):
            self.assertEqual(self.request("/api/favorites", {"op": "add", "name": "a", "lat": 1, "lon": 2})[0], 200)
            self.assertEqual(self.request("/api/favorites", {"op": "add", "name": "b", "lat": 3, "lon": 4})[0], 400)

    def test_state_reports_defaults_label_and_tunnels(self):
        state = self.request()[1]
        self.assertEqual((state["defaults"], state["label"], state["tunnels"]), (None, None, None))
        with patch.dict(os.environ, {"SIMLOCATION_DEFAULT_LAT": "31.230416", "SIMLOCATION_DEFAULT_LON": "121.473701"}):
            self.assertEqual(self.request()[1]["defaults"], {"lat": 31.230416, "lon": 121.473701})
        with patch.dict(os.environ, {"SIMLOCATION_DEFAULT_LAT": "95", "SIMLOCATION_DEFAULT_LON": "1"}):
            self.assertIsNone(self.request()[1]["defaults"])
        with patch.dict(os.environ, {"SIMLOCATION_WEB_LABEL": "  hk\tvps  "}):
            self.assertEqual(web.WebConsole(cli, "/unused", self.app.args).label, "hk vps")
        response = MagicMock()
        response.json.return_value = {PHONE: [{"tunnel-address": "fd00::1", "tunnel-port": 5000}],
                                      OTHER: [], "not-a-udid": [{"tunnel-address": "fd00::2", "tunnel-port": 1}]}
        self.tunneld.reset_mock()
        self.tunneld.side_effect = None
        self.tunneld.return_value = response
        self.app.tunnel_cache = None
        self.assertEqual(self.request()[1]["tunnels"], [PHONE])
        self.request()
        self.assertEqual(self.tunneld.call_count, 1, "cached between polls")

    def test_picker_opens_on_the_default_location(self):
        status, page = self.request("/picker?embed=1")
        self.assertEqual(status, 200)
        self.assertIn("setView([39.909, 116.397], 12)", page)
        with patch.dict(os.environ, {"SIMLOCATION_DEFAULT_LAT": "31.230416", "SIMLOCATION_DEFAULT_LON": "121.473701"}):
            self.assertIn("setView([31.230416, 121.473701], 15)", self.request("/picker?embed=1")[1])
            self.assertIn("GCJ.toMap([31.230416, 121.473701])", cli.render_map_html("key").decode())
