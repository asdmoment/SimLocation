"""Route geometry, input validation, and playback behavior without device I/O.

Ported from hoicau's work in PR #1; adapted to the current CLI structure.
"""

import asyncio
import contextlib
import io
import json
import math
import os
import random
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from test_simlocation import simlocation as cli


class RouteGeometryTests(unittest.TestCase):
    def test_distance_and_interpolation_across_multiple_segments(self):
        route = cli.Route([[0, 0], [0, 1], [1, 1]])
        self.assertAlmostEqual(route.total_m, 222390.16, places=1)
        self.assertEqual(route.position(0), (0, 0))
        self.assertEqual(route.position(route.total_m), (1, 1))
        self.assertAlmostEqual(route.position(route.total_m / 4)[1], 0.5)
        self.assertAlmostEqual(route.position(route.total_m * 3 / 4)[0], 0.5)

    def test_antimeridian_uses_the_short_path(self):
        route = cli.Route([[0, 179.9], [0, -179.9]])
        self.assertLess(route.total_m, 23000)
        lat, lon = route.position(route.total_m / 2)
        self.assertAlmostEqual(lat, 0)
        self.assertAlmostEqual(abs(lon), 180)

    def test_polar_route_remains_finite(self):
        route = cli.Route([[89.9, 0], [89.9, 180]])
        lat, lon = route.position(route.total_m / 2)
        self.assertGreater(lat, 89.99)
        self.assertTrue(math.isfinite(lon))

    def test_duplicates_are_removed_and_loop_gets_a_return_segment(self):
        route = cli.Route([[0, 0], [0, 0], [0, 1]], loop=True)
        self.assertEqual(route.points, [(0, 0), (0, 1), (0, 0)])
        self.assertEqual(route.waypoints(), [(0, 0), (0, 1)])
        self.assertAlmostEqual(route.position(route.total_m * 0.75)[1], 0.5)
        # An explicitly closed ring must not gain a second closing segment.
        closed = cli.Route([[0, 0], [0, 1], [0, 0]], loop=True)
        self.assertEqual(route.points, closed.points)

    def test_invalid_routes_are_rejected(self):
        for points in ([], [[0, 0]], [[0, 0], [0, 0]], [[0, 0], [0, 360]],
                       [[False, 0], [1, 1]], [[0, 0], [float("nan"), 1]],
                       [[0, 0], [0, float("inf")]], [[0, 0], [0, 180]],
                       [[0, 0], [1]], [[0, 0], None], [[0, 0], "01"],
                       {"points": []}):
            with self.subTest(points=points), self.assertRaises(ValueError):
                cli.Route(points)

    def test_point_limit_is_enforced(self):
        too_many = [[0, index / 100000] for index in range(cli.MAX_ROUTE_POINTS + 1)]
        with self.assertRaises(ValueError):
            cli.Route(too_many)


class RouteLoadingTests(unittest.TestCase):
    def test_json_shapes_and_namespaced_gpx(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "route.json"
            for data in ([[1, 2], [3, 4]], {"points": [[1, 2], [3, 4]]}):
                path.write_text(json.dumps(data))
                self.assertEqual(cli.load_route(path).points, [(1, 2), (3, 4)])
            path = path.with_suffix(".gpx")
            for outer, inner in (("trkseg", "trkpt"), ("rte", "rtept")):
                path.write_text(
                    f'<gpx xmlns="http://www.topografix.com/GPX/1/1"><{outer}>'
                    f'<{inner} lat="1" lon="2"/><{inner} lat="3" lon="4"/>'
                    f"</{outer}></gpx>"
                )
                self.assertEqual(cli.load_route(path).points, [(1, 2), (3, 4)])

    def test_invalid_files_and_disconnected_gpx_segments(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "route.gpx"
            for data in ("<gpx>", "<gpx/>",
                         '<gpx><trkseg><trkpt lat="1" lon="2"/></trkseg>'
                         '<trkseg><trkpt lat="3" lon="4"/></trkseg></gpx>'):
                path.write_text(data)
                with self.assertRaises(ValueError):
                    cli.load_route(path)
            path.unlink()
            with self.assertRaises(ValueError):
                cli.load_route(path)
            path = path.with_suffix(".json")
            for data in ('{"points": null}', "invalid", "[1, 2]"):
                path.write_text(data)
                with self.assertRaises(ValueError):
                    cli.load_route(path)

    def test_entity_expansion_is_refused(self):
        """A 1 KB GPX must not be able to allocate hundreds of MB."""
        bomb = (
            '<!DOCTYPE gpx [<!ENTITY a "aaaaaaaaaa">'
            '<!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">]>'
            '<gpx><trkseg><trkpt lat="&b;" lon="2"/></trkseg></gpx>'
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bomb.gpx"
            path.write_text(bomb)
            with self.assertRaises(ValueError):
                cli.load_route(path)

    def test_oversized_file_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "big.json"
            path.write_bytes(b" " * (cli.MAX_ROUTE_BYTES + 1))
            with self.assertRaises(ValueError):
                cli.load_route(path)

    def test_loop_round_trips_through_the_worker_snapshot(self):
        route = cli.Route([[0, 0], [0, 0.001], [0.001, 0.001]], loop=True)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "route.json"
            path.write_text(json.dumps({"points": route.waypoints()}))
            self.assertEqual(cli.load_route(path, loop=True).points, route.points)


class RouteArgumentTests(unittest.TestCase):
    def test_noise_defaults_and_explicit_options(self):
        args = cli.parse_args(["route"])
        self.assertEqual((args.speed_noise, args.position_noise), (0, 0))
        args = cli.parse_args(["route", "--speed-noise", "15", "--position-noise", "3"])
        self.assertEqual((args.speed_noise, args.position_noise), (15, 3))

    def test_invalid_noise_is_rejected(self):
        with contextlib.redirect_stderr(io.StringIO()):
            for flag in ("--speed-noise", "--position-noise"):
                for value in ("-1", "101", "nan", "inf"):
                    with self.subTest(flag=flag, value=value), self.assertRaises(SystemExit):
                        cli.parse_args(["route", flag, value])

    def test_shared_options_survive_on_either_side_of_the_subcommand(self):
        args = cli.parse_args(["--device", "phone", "route", "--speed", "8", "--loop"])
        self.assertEqual(
            (args.device, args.speed, args.loop, args.file), ("phone", 8, True, None)
        )
        args = cli.parse_args(["route", "--loop", "--device", "phone"])
        self.assertEqual(args.device, "phone")

    def test_bad_input_fails_before_any_device_work(self):
        with contextlib.redirect_stderr(io.StringIO()):
            for argv in (["route", "--speed", "nan"], ["route", "--speed", "0"],
                         ["route", "--speed", "-1"], ["route", "--speed", "1001"],
                         ["route", "missing.json"], ["--_hold-session", "route"],
                         ["route", "missing.json", "--pick-only"]):
                with self.subTest(argv=argv), self.assertRaises(SystemExit) as raised:
                    cli.parse_args(argv)
                self.assertEqual(raised.exception.code, 2)

    def test_route_accepts_the_shared_map_server_options(self):
        args = cli.parse_args(["route", "--remote", "--port", "8765"])
        self.assertEqual(cli.map_bind_from_args(args), ("0.0.0.0", False))
        self.assertEqual(args.port, 8765)


class RouteSnapshotTests(unittest.TestCase):
    def test_snapshot_propagates_options_and_is_removed_after_start(self):
        with tempfile.TemporaryDirectory() as directory:
            route = cli.Route([[1, 2], [3, 4]], loop=True)

            def start(*args, **kwargs):
                snapshot = kwargs["route_file"]
                self.assertEqual(cli.load_route(snapshot, loop=True).points, route.points)
                self.assertEqual((kwargs["speed_kmh"], kwargs["loop_route"]), (8, True))
                self.assertEqual((kwargs["speed_noise"], kwargs["position_noise"]), (15, 3))
                return True

            with (
                mock.patch.object(cli, "RUNTIME_DIR", Path(directory)),
                mock.patch.object(cli, "resolve_device_udid", return_value="PHONE"),
                mock.patch.object(cli, "start_hold_session", side_effect=start),
                mock.patch.object(cli, "log_message"),
            ):
                cli.auto_set_route(route, 8, True, "/unused/pmd3", device_flag="phone",
                                   speed_noise=15, position_noise=3)
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_snapshot_is_removed_when_the_session_fails_to_start(self):
        with tempfile.TemporaryDirectory() as directory:
            with (
                mock.patch.object(cli, "RUNTIME_DIR", Path(directory)),
                mock.patch.object(cli, "resolve_device_udid", return_value="PHONE"),
                mock.patch.object(cli, "start_hold_session", return_value=False),
                mock.patch.object(cli, "log_message"),
            ):
                with self.assertRaises(SystemExit):
                    cli.auto_set_route(cli.Route([[0, 0], [0, 1]]), 5, False, "/unused/pmd3")
            self.assertEqual(list(Path(directory).iterdir()), [])


class RoutePickerTests(unittest.TestCase):
    def test_bad_polyline_is_rejected_without_closing_the_picker(self):
        handler = object.__new__(cli._MapRequestHandler)
        handler.path = "/confirm"
        handler.server = mock.Mock(
            route_mode=True, loop_route=False, picked_coords=None, access_token=None
        )
        handler.send_error = mock.Mock()
        for payload in ({"points": [[0, 0]]}, {"points": [[0, 0], [float("inf"), 1]]}, []):
            body = json.dumps(payload).encode()
            handler.headers = {"Content-Length": str(len(body))}
            handler.rfile = io.BytesIO(body)
            handler.do_POST()
        self.assertEqual(handler.send_error.call_count, 3)
        self.assertIsNone(handler.server.picked_coords)
        handler.server.shutdown.assert_not_called()

    def test_route_payload_may_exceed_the_single_coordinate_body_cap(self):
        points = [[0, index / 100000] for index in range(2000)]
        body = json.dumps({"points": points}).encode()
        self.assertGreater(len(body), cli.MAP_MAX_BODY_BYTES)
        handler = object.__new__(cli._MapRequestHandler)
        handler.path = "/confirm"
        handler.server = mock.Mock(
            route_mode=True, loop_route=False, picked_coords=None, access_token=None
        )
        handler.send_error = mock.Mock()
        handler.headers = {"Content-Length": str(len(body))}
        handler.rfile = io.BytesIO(body)
        handler.send_response = mock.Mock()
        handler.send_header = mock.Mock()
        handler.end_headers = mock.Mock()
        handler.wfile = io.BytesIO()
        handler.do_POST()
        handler.send_error.assert_not_called()
        self.assertEqual(len(handler.server.picked_coords.points), len(points))


class RouteNoiseTests(unittest.TestCase):
    def test_noise_bounds_continuity_and_nonconstant_values(self):
        noise = cli.RouteNoise(random.Random(42))
        samples = [noise.sample(i / 100) for i in range(10001)]
        for speed, integral, east, north in samples:
            self.assertLessEqual(abs(speed), 1)
            self.assertLessEqual(math.hypot(east, north), 1 + 1e-12)
        for a, b in zip(samples, samples[1:]):
            self.assertLess(abs(b[0] - a[0]), .004)
            self.assertLess(math.hypot(b[2] - a[2], b[3] - a[3]), .004)
        self.assertGreater(max(s[0] for s in samples) - min(s[0] for s in samples), .5)

    def test_integral_matches_numerical_integration_and_skipped_ticks(self):
        noise = cli.RouteNoise(random.Random(42))
        previous = noise.sample(0)[0]
        area = 0
        for i in range(1, 3726):
            speed, integral, *_ = noise.sample(i / 100)
            area += (previous + speed) / 2 * .01
            previous = speed
        self.assertAlmostEqual(area, integral, places=5)
        sparse = cli.RouteNoise(random.Random(42)).sample(37.25)
        self.assertEqual(sparse, noise.sample(37.25))

    def test_offsets_are_bounded_at_poles_and_antimeridian(self):
        for point in ((0, 180), (90, 0), (-90, 180), (45, -179.99999)):
            shifted = cli.offset_coordinate(point, 60, 80)
            cli.validate_coordinates(*shifted)
            self.assertAlmostEqual(cli.route_distance(point, shifted), 100, places=5)
            self.assertEqual(cli.offset_coordinate(point, 0, 0), point)

    def test_child_command_preserves_noise(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "route.json"
            path.write_text('{"points": [[0, 0], [0, 1]]}')
            with (
                mock.patch.object(cli, "RUNTIME_DIR", Path(directory)),
                mock.patch.object(cli, "stop_hold_session"),
                mock.patch.object(cli.subprocess, "Popen") as spawn,
                mock.patch.object(cli, "wait_for_hold_session", return_value=True),
                mock.patch.object(cli.threading, "Thread"),
            ):
                cli.start_hold_session(0, 0, "pmd3", "auto", "phone", route_file=path,
                                       speed_noise=15, position_noise=3, loop_route=True)
            args = cli.parse_args(spawn.call_args.args[0][2:])
            self.assertEqual((args.speed_noise, args.position_noise, args.loop), (15, 3, True))


class PlaybackTests(unittest.IsolatedAsyncioTestCase):
    async def test_noise_changes_playback_with_bounded_offset_and_exact_endpoint(self):
        for loop in (False, True):
            route = cli.Route([[0, 0], [0, 0.0003]], loop=loop)
            stop = asyncio.Event()
            now, updates, positions = [0.0], [], []

            async def set_position(lat, lon):
                positions.append((lat, lon))
                now[0] += .3
                if (not loop and (lat, lon) == route.points[-1]) or len(positions) >= 90:
                    stop.set()

            async def wait(waiter, timeout):
                waiter.close()
                now[0] += timeout
                raise asyncio.TimeoutError

            with (
                mock.patch.object(cli.asyncio, "wait_for", side_effect=wait),
                mock.patch.object(cli, "write_state", side_effect=lambda p, s: updates.append(s.copy())),
            ):
                await cli.play_route(
                    mock.Mock(set=set_position), route, 3.6, loop, stop, {}, Path("unused"),
                    clock=lambda: now[0], speed_noise=20, position_noise=3, rng=random.Random(42),
                )
            self.assertEqual(positions[0], route.points[0])
            offsets = []
            for point, state in zip(positions, updates):
                offset = cli.route_distance(point, route.position(state["distance_m"]))
                offsets.append(offset)
                self.assertLessEqual(offset, 3 + 1e-6)
                if state["route_phase"] == "moving":
                    self.assertTrue(2.88 <= state["current_speed_kmh"] <= 4.32)
            self.assertGreater(max(offsets), .1)
            self.assertNotEqual(updates[10]["current_speed_kmh"], 3.6)
            if loop:
                self.assertGreater(updates[-1]["lap"], 1)
                self.assertEqual(updates[-1]["route_phase"], "moving")
            else:
                self.assertEqual(positions[-1], route.points[-1])
                self.assertEqual(updates[-1]["route_phase"], "completed")
                self.assertEqual(updates[-1]["current_speed_kmh"], 0)

    async def test_stop_during_the_interval_returns_promptly(self):
        route = cli.Route([[0, 0], [0, 1]])
        stop = asyncio.Event()
        simulation = mock.Mock(set=mock.AsyncMock())
        with mock.patch.object(cli, "write_state"):
            task = asyncio.create_task(
                cli.play_route(simulation, route, 5, False, stop, {}, Path("unused"))
            )
            await asyncio.sleep(0)
            stop.set()
            await asyncio.wait_for(task, timeout=0.1)
        simulation.set.assert_awaited_once()

    async def test_elapsed_time_controls_speed_and_the_endpoint_is_held(self):
        route = cli.Route([[0, 0], [0, 0.0001]])
        stop = asyncio.Event()
        now = [0.0]
        positions, updates = [], []

        async def set_position(lat, lon):
            positions.append((lat, lon))
            now[0] += 0.2  # Simulated device latency must not slow the route down.
            if (lat, lon) == route.points[-1]:
                stop.set()

        async def wait(waiter, timeout):
            waiter.close()
            now[0] += timeout
            raise asyncio.TimeoutError

        with (
            mock.patch.object(cli.asyncio, "wait_for", side_effect=wait),
            mock.patch.object(
                cli, "write_state", side_effect=lambda p, s: updates.append(s.copy())
            ),
        ):
            await cli.play_route(
                mock.Mock(set=set_position), route, 3.6, False, stop, {},
                Path("unused"), clock=lambda: now[0],
            )
        # 3.6 km/h is 1 m/s, and 1.2 s of wall time elapsed between the calls.
        self.assertAlmostEqual(cli.route_distance(positions[0], positions[1]), 1.2)
        self.assertEqual(positions[-1], route.points[-1])
        self.assertEqual(updates[-1]["route_phase"], "completed")
        self.assertEqual(updates[-1]["progress"], 1)

    async def test_loop_advances_laps(self):
        route = cli.Route([[0, 0], [0, 0.0001]], loop=True)
        stop = asyncio.Event()
        now = [0.0]
        updates = []

        async def wait(waiter, timeout):
            waiter.close()
            now[0] += route.total_m * 1.5
            if len(updates) == 2:
                stop.set()
            raise asyncio.TimeoutError

        with (
            mock.patch.object(cli.asyncio, "wait_for", side_effect=wait),
            mock.patch.object(
                cli, "write_state", side_effect=lambda p, s: updates.append(s.copy())
            ),
        ):
            await cli.play_route(
                mock.Mock(set=mock.AsyncMock()), route, 3.6, True, stop, {},
                Path("unused"), clock=lambda: now[0],
            )
        self.assertEqual(len(updates), 2)
        self.assertEqual(updates[-1]["lap"], 2)
        self.assertAlmostEqual(updates[-1]["progress"], 0.5)

    async def test_device_failure_still_attempts_a_clear(self):
        simulation = mock.AsyncMock()
        simulation.__aenter__.return_value = simulation
        simulation.set.side_effect = [None, RuntimeError("disconnected")]
        rsd, dvt = mock.AsyncMock(), mock.AsyncMock()
        with (
            mock.patch.object(cli, "RemoteServiceDiscoveryService", return_value=rsd),
            mock.patch.object(cli, "DvtSecureSocketProxyService", return_value=dvt),
            mock.patch.object(cli, "LocationSimulation", return_value=simulation),
            mock.patch.object(cli, "write_state"),
            mock.patch.object(cli, "log_message"),
        ):
            with self.assertRaisesRegex(RuntimeError, "disconnected"):
                await cli._hold_dvt_location_session(
                    ("::1", 1234), 0, 0, Path("unused"),
                    route=cli.Route([[0, 0], [0, 1]]),
                )
        simulation.clear.assert_awaited_once()


class ReconnectTests(unittest.IsolatedAsyncioTestCase):
    """A session that drops after it was ready rebuilds itself from a new tunnel."""

    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.dir = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.state_path = self.dir / "state.json"
        for name, value in (("HOLD_WATCH_INTERVAL_SECONDS", 0.01), ("RECONNECT_RETRY_SECONDS", 0.01)):
            self.stack.enter_context(mock.patch.object(cli, name, value))
        self.logs = []
        self.stack.enter_context(mock.patch.object(cli, "log_message", lambda message, *_a: self.logs.append(message)))
        self.stack.enter_context(mock.patch.object(cli, "RemoteServiceDiscoveryService", return_value=mock.AsyncMock()))
        self.stack.enter_context(mock.patch.object(cli, "DvtSecureSocketProxyService", return_value=mock.AsyncMock()))
        self.stop = asyncio.Event()

    def tearDown(self):
        self.stack.close()

    def simulations(self, *simulations):
        for simulation in simulations:
            simulation.__aenter__.return_value = simulation
        self.stack.enter_context(mock.patch.object(cli, "LocationSimulation", side_effect=list(simulations)))

    def hold(self, **kwargs):
        return cli._hold_dvt_location_session(
            ("fd00::1", 1001), "12.5", "123.45", self.state_path, stop_event=self.stop,
            udid="UDID", connection_mode="rsd", **kwargs)

    async def test_lost_tunnel_is_rebuilt_and_the_point_set_again(self):
        first, second = mock.AsyncMock(), mock.AsyncMock()
        second.set.side_effect = lambda *_a: self.stop.set()
        self.simulations(first, second)
        with mock.patch.object(cli, "snapshot_rsd_candidates", return_value=[]), \
             mock.patch.object(cli, "acquire_rsd", side_effect=[None, ("fd00::2", "1002")]) as acquire:
            await self.hold(reconnect_seconds=60)
        first.set.assert_awaited_once_with(12.5, 123.45)
        second.set.assert_awaited_once_with(12.5, 123.45)
        first.clear.assert_not_awaited()  # a reconnecting session keeps the point
        second.clear.assert_awaited_once()
        self.assertEqual(acquire.call_count, 2)
        state = cli.read_state(self.state_path)
        self.assertEqual((state["status"], state["reconnects"], state["rsd_address"]), ("cleared", 1, "fd00::2"))
        self.assertNotIn("link", state)
        self.assertTrue(any("自动重连" in line for line in self.logs))

    async def test_failed_resend_counts_as_a_drop(self):
        lost = asyncio.Event()
        with mock.patch.object(cli, "snapshot_rsd_candidates", return_value=[("fd00::1", "1001")]):
            simulation = mock.AsyncMock()
            simulation.set.side_effect = OSError("broken pipe")
            with self.assertRaisesRegex(cli.SessionLost, "broken pipe"):
                await cli.watch_session(simulation, ("fd00::1", 1001), "UDID", {"lat": "1", "lon": "2"},
                                        lost, lambda: True, interval=0.01)
            # tunneld not answering is not proof of anything; a busy route is not resent.
            simulation.set.reset_mock()
        with mock.patch.object(cli, "snapshot_rsd_candidates", return_value=None):
            task = asyncio.ensure_future(cli.watch_session(simulation, ("fd00::1", 1001), "UDID", {}, lost,
                                                           lambda: False, interval=0.01))
            await asyncio.sleep(0.05)
            lost.set()
            await task
        simulation.set.assert_not_awaited()

    async def test_a_resend_error_without_a_message_is_named(self):
        # asyncio.wait_for times out with a bare TimeoutError; the reason must still say so.
        with mock.patch.object(cli, "snapshot_rsd_candidates", return_value=[("fd00::1", "1001")]):
            simulation = mock.AsyncMock()
            simulation.set.side_effect = TimeoutError()
            with self.assertRaisesRegex(cli.SessionLost, "重发定位失败: TimeoutError$"):
                await cli.watch_session(simulation, ("fd00::1", 1001), "UDID", {"lat": "1", "lon": "2"},
                                        asyncio.Event(), lambda: True, interval=0.01)

    async def test_gives_up_after_the_window_and_reports_why(self):
        self.simulations(mock.AsyncMock())
        with mock.patch.object(cli, "snapshot_rsd_candidates", return_value=[]), \
             mock.patch.object(cli, "acquire_rsd", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "没能重连.*不再提供"):
                await self.hold(reconnect_seconds=0.05)
        state = cli.read_state(self.state_path)
        self.assertEqual(state["link"], "reconnecting")

    async def test_stop_while_reconnecting_ends_without_touching_the_device(self):
        first = mock.AsyncMock()
        self.simulations(first)

        def no_tunnel(*_a):
            self.stop.set()
            return None

        with mock.patch.object(cli, "snapshot_rsd_candidates", return_value=[]), \
             mock.patch.object(cli, "acquire_rsd", side_effect=no_tunnel):
            await self.hold(reconnect_seconds=60)
        first.clear.assert_not_awaited()
        self.assertFalse(cli.read_state(self.state_path).get("clear_confirmed"))

    async def test_failure_before_ready_is_not_retried(self):
        first = mock.AsyncMock()
        first.set.side_effect = RuntimeError("no developer image")
        self.simulations(first)
        with mock.patch.object(cli, "acquire_rsd") as acquire:
            with self.assertRaisesRegex(RuntimeError, "no developer image"):
                await self.hold(reconnect_seconds=60)
        acquire.assert_not_called()

    async def test_resumed_route_follows_wall_time(self):
        simulation = mock.AsyncMock()
        simulation.set.side_effect = lambda *_a: None
        state = {}
        stop = asyncio.Event()

        async def set_once(*_a):
            if simulation.set.await_count >= 1:
                stop.set()

        simulation.set.side_effect = set_once
        await cli.play_route(simulation, cli.Route([[0, 0], [0, 1]]), 3.6, False, stop, state,
                             self.state_path, clock=lambda: 130.0, started=30.0)
        self.assertAlmostEqual(state["distance_m"], 100.0)

    def test_window_comes_from_the_environment(self):
        with mock.patch.dict(os.environ, {"SIMLOCATION_RECONNECT_SECONDS": "0"}):
            self.assertEqual(cli.get_reconnect_seconds(), 0)
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SIMLOCATION_RECONNECT_SECONDS", None)
            self.assertEqual(cli.get_reconnect_seconds(), cli.DEFAULT_RECONNECT_SECONDS)
        for bad in ("-1", "soon", "inf", "nan"):
            with mock.patch.dict(os.environ, {"SIMLOCATION_RECONNECT_SECONDS": bad}):
                with self.assertRaises(ValueError, msg=bad):
                    cli.get_reconnect_seconds()


class RouteStatusTests(unittest.TestCase):
    def test_current_speed_is_shown_when_noise_is_active(self):
        text = self.describe({"mode": "route", "speed_kmh": 12, "current_speed_kmh": 11.5})
        self.assertIn("11.5 km/h", text)

    def describe(self, extra):
        state = {"status": "ready", "pid": os_pid(), "lat": "1.000000", "lon": "2.000000"}
        state.update(extra)
        return cli.describe_session_state(state)

    def test_moving_progress_and_lap_are_shown(self):
        text = self.describe(
            {"mode": "route", "route_phase": "moving", "progress": 0.42,
             "speed_kmh": 12, "loop": True, "lap": 3}
        )
        self.assertIn("移动中 42%", text)
        self.assertIn("12 km/h", text)
        self.assertIn("第 3 圈", text)

    def test_completed_route_reports_a_held_endpoint(self):
        text = self.describe({"mode": "route", "route_phase": "completed"})
        self.assertIn("已到终点", text)

    def test_reconnecting_session_says_so(self):
        text = self.describe({"link": "reconnecting", "reconnect_since": "2026-11-03T10:00:00",
                              "reconnect_deadline": "2026-11-03T10:30:00"})
        self.assertTrue(text.startswith("重连中 (1.000000, 2.000000)"))
        self.assertIn("10:30:00", text)

    def test_plain_session_is_unchanged(self):
        self.assertEqual(self.describe({}), "ready (1.000000, 2.000000)")


def os_pid():
    import os
    return os.getpid()


if __name__ == "__main__":
    unittest.main()
