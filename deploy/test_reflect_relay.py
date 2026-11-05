#!/usr/bin/env python3
import asyncio
import contextlib
import importlib.util
from pathlib import Path
import socket
import unittest
from unittest.mock import AsyncMock, patch

spec = importlib.util.spec_from_file_location('relay', Path(__file__).with_name('reflect_relay.py'))
relay = importlib.util.module_from_spec(spec)
spec.loader.exec_module(relay)


async def stream_pair():
    a, b = socket.socketpair()
    return await asyncio.open_connection(sock=a), await asyncio.open_connection(sock=b)


class FrameTests(unittest.IsolatedAsyncioTestCase):
    async def test_rejects_non_dict(self):
        r = asyncio.StreamReader(); r.feed_data(b'[]\n')
        with self.assertRaises(ValueError): await relay.receive(r)

    async def test_rejects_large_frame(self):
        r = asyncio.StreamReader(limit=relay.FRAME_LIMIT * 2)
        r.feed_data(b'x' * (relay.FRAME_LIMIT + 1) + b'\n')
        with self.assertRaises(ValueError): await relay.receive(r)

    async def test_eof(self):
        r = asyncio.StreamReader(); r.feed_eof()
        with self.assertRaises(EOFError): await relay.receive(r)

    def test_port_policy(self):
        self.assertTrue(relay.valid_port(49152))
        self.assertTrue(relay.valid_port(52000, data=True))
        for value in [22, 6171, 8446, 49152, 65536, '52000', True]:
            self.assertFalse(relay.valid_port(value, data=True))


class ReconnectTests(unittest.IsolatedAsyncioTestCase):
    async def test_disconnect_retries_when_enabled(self):
        attempt = AsyncMock(side_effect=[EOFError(), asyncio.CancelledError()])
        with patch.object(relay, 'run_phone_once', attempt), \
             patch.object(relay.asyncio, 'sleep', AsyncMock()) as sleep:
            with self.assertRaises(asyncio.CancelledError):
                await relay.run_phone({'reconnect': True})
            self.assertEqual(attempt.await_count, 2)
            sleep.assert_awaited_once_with(5)

    async def test_certificate_failure_never_retries(self):
        attempt = AsyncMock(side_effect=relay.ssl.SSLCertVerificationError('fixture'))
        with patch.object(relay, 'run_phone_once', attempt), \
             patch.object(relay.asyncio, 'sleep', AsyncMock()) as sleep:
            with self.assertRaises(relay.ssl.SSLCertVerificationError):
                await relay.run_phone({'reconnect': True})
            sleep.assert_not_called()

    async def test_network_timeout_is_not_session_expiry(self):
        with patch.object(relay, 'run_phone_once', AsyncMock(side_effect=TimeoutError())):
            with self.assertRaises(TimeoutError):
                await relay.run_phone({'reconnect': False})


class MuxTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.echo_tasks = set()
        async def echo(r, w):
            task = asyncio.current_task(); self.echo_tasks.add(task)
            try:
                while data := await r.read(16384):
                    w.write(data); await w.drain()
            finally:
                w.close(); await w.wait_closed(); self.echo_tasks.discard(task)
        self.echo = await asyncio.start_server(echo, '127.0.0.1', 0)
        self.echo_port = self.echo.sockets[0].getsockname()[1]
        (sr, sw), (pr, pw) = await stream_pair()
        self.server = relay.Mux(sr, sw)
        self.phone = relay.Mux(pr, pw, phone=True)
        async def dial(port):
            self.assertIn(port, self.phone.allowed)
            return await asyncio.open_connection('127.0.0.1', self.echo_port)
        self.phone.dial_target = dial
        self.runners = [asyncio.create_task(self.server.run()), asyncio.create_task(self.phone.run())]
        self.local_tasks = []
        self.local_writers = []

    async def asyncTearDown(self):
        for w in self.local_writers: w.close()
        for t in self.local_tasks + self.runners: t.cancel()
        await asyncio.gather(*self.local_tasks, *self.runners, return_exceptions=True)
        self.echo.close(); await self.echo.wait_closed()
        for t in list(self.echo_tasks): t.cancel()
        await asyncio.gather(*self.echo_tasks, return_exceptions=True)

    async def open(self, port):
        (r, w), (local_r, local_w) = await stream_pair()
        self.local_writers.extend([w, local_w])
        task = asyncio.create_task(self.server.open_local(port, local_r, local_w))
        self.local_tasks.append(task)
        self.assertEqual(await relay.receive(r, 3), {'ok': True})
        return r, w

    async def test_control_and_data_roundtrip(self):
        r, w = await self.open(49152)
        data = bytes(range(256)) * 512
        w.write(data); await w.drain()
        self.assertEqual(await asyncio.wait_for(r.readexactly(len(data)), 5), data)
        await self.server.grant(52000)
        r2, w2 = await self.open(52000)
        w2.write(b'PSK-TLS-stream-fixture'); await w2.drain()
        self.assertEqual(await asyncio.wait_for(r2.readexactly(22), 3), b'PSK-TLS-stream-fixture')
        w.write(b'still-open'); await w.drain()
        self.assertEqual(await asyncio.wait_for(r.readexactly(10), 3), b'still-open')

    async def test_refuses_ungranted_port(self):
        with self.assertRaises(ValueError):
            await self.server.open_local(52000, None, None)

    async def test_refuses_management_ports_and_live_second_grant(self):
        for port in [22, 6171, 8446, 49152, 65536, True]:
            with self.assertRaises(ValueError): await self.server.grant(port)
        await self.server.grant(52000)
        await self.open(52000)
        with self.assertRaises(ValueError): await self.server.grant(52001)

    async def test_replaces_unused_grant_and_revokes_old_port(self):
        await self.open(49152)
        await self.server.grant(52000)
        await self.server.grant(52001)
        self.assertEqual(self.server.allowed, {49152, 52001})
        self.assertEqual(self.phone.allowed, {49152, 52001})
        with self.assertRaises(ValueError):
            await self.server.open_local(52000, None, None)
        await self.open(52001)

    async def test_phone_refusal_returns_json_before_eof(self):
        async def refused(port):
            raise ConnectionRefusedError('fixture')
        self.phone.dial_target = refused
        (r, w), (local_r, local_w) = await stream_pair()
        self.local_writers.extend([w, local_w])
        task = asyncio.create_task(self.server.open_local(49152, local_r, local_w))
        self.local_tasks.append(task)
        self.assertEqual(await relay.receive(r, 3),
                         {'ok': False, 'error': 'phone_port_refused'})
        await asyncio.wait_for(task, 3)
        self.assertEqual(await r.read(), b'')
        self.assertEqual(self.server.channels, {})

    async def test_phone_timeout_returns_specific_error(self):
        async def timeout(port):
            raise TimeoutError('fixture')
        self.phone.dial_target = timeout
        (r, w), (local_r, local_w) = await stream_pair()
        self.local_writers.extend([w, local_w])
        task = asyncio.create_task(self.server.open_local(49152, local_r, local_w))
        self.local_tasks.append(task)
        self.assertEqual(await relay.receive(r, 3),
                         {'ok': False, 'error': 'phone_port_timeout'})
        await asyncio.wait_for(task, 3)

    async def test_phone_rejects_unauthorized_open(self):
        with self.assertRaises(ValueError):
            await self.phone.dispatch({'op': 'open', 'id': 4, 'port': 6171})
        self.assertEqual(self.phone.opening, set())

    async def test_disconnect_closes_local_stream(self):
        r, w = await self.open(49152)
        self.phone.writer.close()
        self.assertEqual(await asyncio.wait_for(r.read(1), 3), b'')

    async def test_rejects_invalid_base64(self):
        with self.assertRaises(ValueError):
            await self.phone.dispatch({'op': 'data', 'id': 1, 'data': '!@#$'})


if __name__ == '__main__':
    unittest.main(verbosity=2)
