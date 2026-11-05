import asyncio
from pathlib import Path
import sys
import unittest
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import reflect_tunneld as adapter


def writer():
    w = Mock()
    w.drain = AsyncMock()
    w.wait_closed = AsyncMock()
    w.start_tls = AsyncMock()
    return w


class RelayRequestTests(unittest.IsolatedAsyncioTestCase):
    async def request(self, data):
        r = asyncio.StreamReader()
        r.feed_data(data)
        r.feed_eof()
        w = writer()
        with patch.object(adapter.asyncio, 'open_unix_connection', AsyncMock(return_value=(r, w))):
            try:
                return await adapter.relay_request('/fixture.sock', 'open', 49152)
            except BaseException:
                w.close.assert_called_once()
                raise

    async def test_success_keeps_stream_open(self):
        r, w = await self.request(b'{"ok":true}\n')
        w.close.assert_not_called()
        self.assertIn(b'49152', w.write.call_args.args[0])

    async def test_phone_refused_is_not_json_error(self):
        with self.assertRaisesRegex(adapter.RelayError, 'phone_port_refused'):
            await self.request(b'{"ok":false,"error":"phone_port_refused"}\n')

    async def test_empty_or_malformed_reply_is_explicit(self):
        for payload in (b'', b'not json\n', b'[]\n'):
            with self.assertRaisesRegex(adapter.RelayError, 'relay_closed_before_reply'):
                await self.request(payload)

    async def test_unknown_error_is_not_echoed_into_logs(self):
        with self.assertRaisesRegex(adapter.RelayError, '^relay_request_rejected$'):
            await self.request(b'{"ok":false,"error":"arbitrary remote text"}\n')


class ProviderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.config = {'udid': '00008160-0000000000000000', 'host': '10.77.77.2',
                       'port': 49152, 'pair_record': Path('/fixture/device.plist'),
                       'relay_socket': '/fixture.sock'}
        self.provider = adapter.ReflectedProvider(self.config)

    async def test_new_pairing_is_rejected_before_io(self):
        with patch.object(adapter, 'relay_request', AsyncMock()) as request:
            with self.assertRaises(ValueError):
                await self.provider.connect(autopair=True)
            request.assert_not_called()

    async def test_only_existing_identity_verified(self):
        p = self.provider
        with patch.object(adapter, 'relay_request', AsyncMock(return_value=(Mock(), writer()))) as request, \
             patch.object(p, '_attempt_pair_verify', AsyncMock()) as hello, \
             patch.object(p, '_validate_pairing', AsyncMock(return_value=True)) as verify, \
             patch.object(p, '_init_client_server_main_encryption_keys', Mock()) as keys:
            await p.connect()
            request.assert_awaited_once_with('/fixture.sock', 'open', 49152)
            hello.assert_awaited_once()
            verify.assert_awaited_once()
            keys.assert_called_once()
            self.assertEqual(p.pair_record_path, self.config['pair_record'])

    async def test_rejected_identity_closes_control(self):
        p = self.provider
        with patch.object(adapter, 'relay_request', AsyncMock(return_value=(Mock(), writer()))), \
             patch.object(p, '_attempt_pair_verify', AsyncMock()), \
             patch.object(p, '_validate_pairing', AsyncMock(return_value=False)), \
             patch.object(p, 'close', AsyncMock()) as close:
            with self.assertRaisesRegex(adapter.RelayError, 'existing_identity_rejected'):
                await p.connect()
            close.assert_awaited_once()

    async def test_data_tunnel_is_registered_and_cleaned(self):
        p = self.provider
        control, stream = writer(), writer()
        tunnel = Mock()
        tunnel.request_tunnel_establish = AsyncMock(return_value={
            'clientParameters': {'address': 'fd00::1', 'mtu': 1280},
            'serverAddress': 'fd00::2', 'serverRSDPort': 62000})
        tunnel.start_tunnel = AsyncMock()
        tunnel.stop_tunnel = AsyncMock()
        tunnel.tun.name = 'simloc-rsd'
        with patch.object(p, 'create_tcp_listener', AsyncMock(return_value={'port': 55000})), \
             patch.object(p, '_set_psk_ciphers', Mock()), \
             patch.object(adapter, 'relay_request', AsyncMock(side_effect=[(Mock(), control), (Mock(), stream)])) as request, \
             patch.object(adapter.ssl, 'SSLContext', Mock()), \
             patch.object(adapter, 'RemotePairingTcpTunnel', Mock(return_value=tunnel)):
            async with p.start_tcp_tunnel() as result:
                self.assertEqual(result.address, 'fd00::2')
                self.assertEqual(result.port, 62000)
                self.assertEqual(result.interface, 'simloc-rsd')
                self.assertIs(result.client, tunnel)
                tunnel.stop_tunnel.assert_not_called()
            self.assertEqual([c.args[1:] for c in request.await_args_list],
                             [('grant', 55000), ('open', 55000)])
            tunnel.stop_tunnel.assert_awaited_once()
            stream.close.assert_called_once()

    async def test_data_port_is_validated_before_grant(self):
        with patch.object(self.provider, 'create_tcp_listener', AsyncMock(return_value={'port': 22})), \
             patch.object(adapter, 'relay_request', AsyncMock()) as request:
            with self.assertRaises(ValueError):
                async with self.provider.start_tcp_tunnel():
                    self.fail('invalid data port accepted')
            request.assert_not_called()

    def test_reuses_local_api_and_replaces_discovery(self):
        runner = adapter.make_runner(self.config)
        self.assertEqual(runner.host, '127.0.0.1')
        self.assertEqual(runner.port, 49151)
        self.assertIsInstance(runner._tunneld_core, adapter.ReflectedCore)
        self.assertFalse(runner._tunneld_core.usb_monitor)


if __name__ == '__main__':
    unittest.main(verbosity=2)
