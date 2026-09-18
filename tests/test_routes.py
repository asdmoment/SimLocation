"""Route geometry, input validation, and session behavior without device I/O."""

import asyncio
import contextlib
import io
import json
import math
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from test_simlocation import load_cli


cli = load_cli()


class RouteTests(unittest.TestCase):
    def test_picker_rejects_bad_input_and_remains_available(self):
        handler = object.__new__(cli._MapRequestHandler)
        handler.path = '/confirm'
        handler.server = mock.Mock(route_mode=True, loop_route=False, picked_coords=None)
        handler.send_error = mock.Mock()
        for payload in ({'points': [[0, 0]]}, {'points': [[0, 0], [float('inf'), 1]]}, []):
            body = json.dumps(payload).encode()
            handler.headers = {'Content-Length': str(len(body))}
            handler.rfile = io.BytesIO(body)
            handler.do_POST()
        self.assertEqual(handler.send_error.call_count, 3)
        self.assertIsNone(handler.server.picked_coords)
        handler.server.shutdown.assert_not_called()

    def test_distance_and_interpolation_across_multiple_segments(self):
        route = cli.Route([[0, 0], [0, 1], [1, 1]])
        self.assertAlmostEqual(route.total_m, 222390.16, places=1)
        self.assertEqual(route.position(0), (0, 0))
        self.assertEqual(route.position(route.total_m), (1, 1))
        self.assertAlmostEqual(route.position(route.total_m / 4)[1], 0.5)
        self.assertAlmostEqual(route.position(route.total_m * 3 / 4)[0], 0.5)

    def test_antimeridian_uses_short_path(self):
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

    def test_duplicates_are_removed_and_loop_has_return_segment(self):
        route = cli.Route([[0, 0], [0, 0], [0, 1]], loop=True)
        self.assertEqual(route.points, [(0, 0), (0, 1), (0, 0)])
        self.assertAlmostEqual(route.position(route.total_m * 0.75)[1], 0.5)
        closed = cli.Route([[0, 0], [0, 1], [0, 0]], loop=True)
        self.assertEqual(route.points, closed.points)

    def test_invalid_routes(self):
        for points in ([], [[0, 0]], [[0, 0], [0, 0]], [[0, 0], [0, 360]],
                       [[False, 0], [1, 1]], [[0, 0], [float('nan'), 1]],
                       [[0, 0], [0, float('inf')]], [[0, 0], [0, 180]],
                       [[0, 0], [1]], [[0, 0], None], {'points': []}):
            with self.subTest(points=points), self.assertRaises(ValueError):
                cli.Route(points)

    def test_json_and_namespaced_gpx(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'route.json'
            for data in ([[1, 2], [3, 4]], {'points': [[1, 2], [3, 4]]}):
                path.write_text(json.dumps(data))
                self.assertEqual(cli.load_route(path).points, [(1, 2), (3, 4)])
            path = path.with_suffix('.gpx')
            for outer, inner in (('trkseg', 'trkpt'), ('rte', 'rtept')):
                path.write_text(f'<gpx xmlns="http://www.topografix.com/GPX/1/1"><{outer}>'
                                f'<{inner} lat="1" lon="2"/><{inner} lat="3" lon="4"/>'
                                f'</{outer}></gpx>')
                self.assertEqual(cli.load_route(path).points, [(1, 2), (3, 4)])

    def test_invalid_files_and_disconnected_gpx_segments(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'route.gpx'
            for data in ('<gpx>', '<gpx/>', '<gpx><trkseg><trkpt lat="1" lon="2"/></trkseg>'
                         '<trkseg><trkpt lat="3" lon="4"/></trkseg></gpx>'):
                path.write_text(data)
                with self.assertRaises(ValueError):
                    cli.load_route(path)
            path.unlink()
            with self.assertRaises(ValueError):
                cli.load_route(path)
            path = path.with_suffix('.json')
            for data in ('{"points": null}', 'invalid', '[1, 2]'):
                path.write_text(data)
                with self.assertRaises(ValueError):
                    cli.load_route(path)

    def test_route_flags_and_input_fail_before_device_io(self):
        args = cli.parse_args(['--device', 'phone', 'route', '--speed', '8', '--loop'])
        self.assertEqual((args.device, args.speed, args.loop, args.file), ('phone', 8, True, None))
        with contextlib.redirect_stderr(io.StringIO()):
            for argv in (['route', '--speed', 'nan'], ['route', '--speed', '0'],
                         ['route', '--speed', '-1'], ['route', '--speed', '1001'],
                         ['route', 'missing.json'], ['--_hold-session', 'route'],
                         ['route', 'missing.json', '--pick-only']):
                with self.subTest(argv=argv), self.assertRaises(SystemExit) as raised:
                    cli.parse_args(argv)
                self.assertEqual(raised.exception.code, 2)

    def test_snapshot_propagates_options_and_is_removed_after_start(self):
        with tempfile.TemporaryDirectory() as directory:
            route = cli.Route([[1, 2], [3, 4]], loop=True)
            def start(*args, **kwargs):
                snapshot = kwargs['route_file']
                self.assertEqual(cli.load_route(snapshot, loop=True).points, route.points)
                self.assertEqual((kwargs['speed_kmh'], kwargs['loop_route']), (8, True))
                return True
            with mock.patch.object(cli, 'RUNTIME_DIR', Path(directory)), \
                    mock.patch.object(cli, 'resolve_device_udid', return_value='PHONE'), \
                    mock.patch.object(cli, 'start_hold_session', side_effect=start), \
                    mock.patch.object(cli, 'log_message'):
                cli.auto_set_route(route, 8, True, '/unused/pmd3', device_flag='phone')
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_snapshot_is_removed_on_failed_start(self):
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(cli, 'RUNTIME_DIR', Path(directory)), \
                    mock.patch.object(cli, 'resolve_device_udid', return_value='PHONE'), \
                    mock.patch.object(cli, 'start_hold_session', return_value=False), \
                    mock.patch.object(cli, 'log_message'):
                with self.assertRaises(SystemExit):
                    cli.auto_set_route(cli.Route([[0, 0], [0, 1]]), 5, False, '/unused/pmd3')
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_loop_at_point_limit_can_be_passed_to_worker(self):
        points = [[0, index / 100000] for index in range(cli.MAX_ROUTE_POINTS)]
        route = cli.Route(points, loop=True)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'route.json'
            path.write_text(json.dumps({'points': route.points[:-1]}))
            self.assertEqual(cli.load_route(path, loop=True).points, route.points)


class PlaybackTests(unittest.IsolatedAsyncioTestCase):
    async def test_stop_during_interval_returns_promptly(self):
        route = cli.Route([[0, 0], [0, 1]])
        stop = asyncio.Event()
        simulation = mock.Mock(set=mock.AsyncMock())
        with mock.patch.object(cli, 'write_state'):
            task = asyncio.create_task(cli.play_route(simulation, route, 5, False,
                                                      stop, {}, Path('unused')))
            await asyncio.sleep(0)
            stop.set()
            await asyncio.wait_for(task, timeout=0.1)
        simulation.set.assert_awaited_once()

    async def test_elapsed_time_controls_speed_and_endpoint_is_held(self):
        route = cli.Route([[0, 0], [0, 0.0001]])
        stop = asyncio.Event()
        now = [0.0]
        positions, updates = [], []

        async def set_position(lat, lon):
            positions.append((lat, lon))
            now[0] += 0.2  # Simulated device call latency must not slow the route.
            if (lat, lon) == route.points[-1]:
                stop.set()

        async def wait(waiter, timeout):
            waiter.close()
            now[0] += timeout
            raise asyncio.TimeoutError

        with mock.patch.object(cli.asyncio, 'wait_for', side_effect=wait), \
                mock.patch.object(cli, 'write_state', side_effect=lambda p, s: updates.append(s.copy())):
            await cli.play_route(mock.Mock(set=set_position), route, 3.6, False,
                                 stop, {}, Path('unused'), clock=lambda: now[0])
        self.assertAlmostEqual(cli.route_distance(positions[0], positions[1]), 1.2)
        self.assertEqual(positions[-1], route.points[-1])
        self.assertEqual(updates[-1]['route_phase'], 'completed')
        self.assertEqual(updates[-1]['progress'], 1)

    async def test_loop_advances_laps_and_stop_interrupts_wait(self):
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

        with mock.patch.object(cli.asyncio, 'wait_for', side_effect=wait), \
                mock.patch.object(cli, 'write_state', side_effect=lambda p, s: updates.append(s.copy())):
            await cli.play_route(mock.Mock(set=mock.AsyncMock()), route, 3.6, True,
                                 stop, {}, Path('unused'), clock=lambda: now[0])
        self.assertEqual(len(updates), 2)
        self.assertEqual(updates[-1]['lap'], 2)
        self.assertAlmostEqual(updates[-1]['progress'], 0.5)

    async def test_device_failure_clears_session(self):
        simulation = mock.AsyncMock()
        simulation.__aenter__.return_value = simulation
        simulation.set.side_effect = [None, RuntimeError('disconnected')]
        rsd, dvt = mock.AsyncMock(), mock.AsyncMock()
        with mock.patch.object(cli, 'RemoteServiceDiscoveryService', return_value=rsd), \
                mock.patch.object(cli, 'DvtSecureSocketProxyService', return_value=dvt), \
                mock.patch.object(cli, 'LocationSimulation', return_value=simulation), \
                mock.patch.object(cli, 'write_state'), mock.patch.object(cli, 'log_message'):
            with self.assertRaisesRegex(RuntimeError, 'disconnected'):
                await cli._hold_dvt_location_session(('::1', 1234), 0, 0, Path('unused'),
                                                     route=cli.Route([[0, 0], [0, 1]]))
        simulation.clear.assert_awaited_once()


if __name__ == '__main__':
    unittest.main()
