import asyncio
import importlib.util
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))  # hold_probe imports the adapter as a sibling module
spec = importlib.util.spec_from_file_location('hold_probe', HERE / 'hold_probe.py')
h = importlib.util.module_from_spec(spec)
spec.loader.exec_module(h)

PEER = 'PeerPublicKeyFixture' + 'A' * 23 + '='


class WireGuardParsingTests(unittest.TestCase):
    def test_endpoint_keeps_only_carrier_prefix_and_port(self):
        self.assertEqual(h.redact_endpoint('198.51.100.34:24972'), '198.51.x.x:24972')
        self.assertEqual(h.redact_endpoint('(none)'), '(none)')
        self.assertEqual(h.redact_endpoint('[2001:db8::10]:5000'), '<redacted>')

    def test_peer_key_never_reaches_the_record(self):
        sample = h.parse_wg(f'{PEER}\t1793623957\n', f'{PEER}\t3735344\t823740\n',
                            f'{PEER}\t198.51.100.34:24972\n')
        self.assertEqual(sample, {'handshake': 1793623957, 'rx': 3735344, 'tx': 823740,
                                  'endpoint': '198.51.x.x:24972'})
        self.assertNotIn(PEER, repr(sample))

    def test_more_than_one_peer_rejected(self):
        two = f'{PEER}\t1\n{PEER}\t2\n'
        with self.assertRaises(ValueError):
            h.parse_wg(two, f'{PEER}\t1\t1\n', f'{PEER}\t1.2.3.4:5\n')


class FakeInfo:
    behaviour = None

    def __init__(self, dvt):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def mach_time_info(self):
        if FakeInfo.behaviour == 'hang':
            await asyncio.sleep(1)
        elif isinstance(FakeInfo.behaviour, BaseException):
            raise FakeInfo.behaviour
        return {}


class CheckTests(unittest.IsolatedAsyncioTestCase):
    async def outcome(self, behaviour, tunnel=None):
        FakeInfo.behaviour = behaviour
        rows = []
        tunnel = tunnel or {'tunnel-address': 'fd00::1', 'tunnel-port': 1}
        with patch.object(h, 'DeviceInfo', FakeInfo), patch.object(h, 'CHECK_TIMEOUT', 0.05), \
             patch.object(h, 'registered_tunnel', return_value=tunnel):
            result = await h.check(object(), lambda kind, **f: rows.append((kind, f)), 'test',
                                   ('fd00::1', 1), 'udid')
        return result, rows[-1][1]

    async def test_answer_on_same_tunnel_is_ok(self):
        result, row = await self.outcome(None)
        self.assertEqual(result, 'ok')
        self.assertTrue(row['same_tunnel'])

    async def test_silence_is_timeout_not_death(self):
        result, row = await self.outcome('hang')
        self.assertEqual(result, 'timeout')

    async def test_closed_connection_is_dead_and_new_tunnel_noticed(self):
        result, row = await self.outcome(ConnectionResetError(),
                                         {'tunnel-address': 'fd00::9', 'tunnel-port': 2})
        self.assertEqual((result, row['error'], row['same_tunnel']),
                         ('dead', 'ConnectionResetError', False))


class ReasonTests(unittest.IsolatedAsyncioTestCase):
    async def test_signal_stop_and_deadline(self):
        loop = asyncio.get_running_loop()
        checks, stop = asyncio.Queue(), asyncio.Event()
        checks.put_nowait('signal')
        self.assertEqual(await h.next_reason(checks, stop, loop, loop.time() + 5), 'signal')
        self.assertEqual(await h.next_reason(checks, stop, loop, loop.time() + 0.01), 'deadline')
        stop.set()
        self.assertIsNone(await h.next_reason(checks, stop, loop, loop.time() + 5))


if __name__ == '__main__':
    unittest.main()
