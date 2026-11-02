import asyncio
import importlib.util
import json
import os
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, call, patch

spec = importlib.util.spec_from_file_location('adapter', Path(__file__).with_name('network_tunneld.py'))
a = importlib.util.module_from_spec(spec)
spec.loader.exec_module(a)


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.record = self.root / 'record.plist'
        self.record.write_bytes(b'fixture')
        self.record.chmod(0o600)
        self.data = {'udid': '00008160-0000000000000000', 'host': '10.77.77.2',
                     'pair_record': str(self.record)}

    def load(self):
        p = self.root / 'config.json'
        p.write_text(json.dumps(self.data))
        return a.load_config(p)

    def test_valid_private_record(self):
        c = self.load()
        self.assertEqual(c['port'], 49152)
        self.assertEqual(a.DirectProvider(c).pair_record_path, self.record)

    def test_public_record_rejected(self):
        self.record.chmod(0o644)
        with self.assertRaises(ValueError): self.load()

    def test_bad_port_rejected(self):
        for port in (0, 65536, True, '49152'):
            self.data['port'] = port
            with self.assertRaises(ValueError): self.load()

    def test_bad_host_and_identifier_rejected(self):
        self.data['host'] = 'arbitrary-hostname'
        with self.assertRaises(ValueError): self.load()
        self.data['host'] = '10.77.77.2'; self.data['udid'] = '../other-device'
        with self.assertRaises(ValueError): self.load()

    def test_upstream_runner_is_loopback_only(self):
        runner = a.make_runner(self.load())
        self.assertEqual(runner.host, '127.0.0.1')
        self.assertFalse(runner._tunneld_core.usb_monitor)
        self.assertFalse(runner._tunneld_core.usbmux_monitor)
        self.assertFalse(runner._tunneld_core.mobdev2_monitor)

    def test_keepalive_is_optional(self):
        self.assertIsNone(self.load()['keepalive'])
        self.data['keepalive'] = {'idle': 30, 'interval': 30, 'count': 40}
        self.assertEqual(self.load()['keepalive'], {'idle': 30, 'interval': 30, 'count': 40})

    def test_bad_keepalive_rejected(self):
        good = {'idle': 30, 'interval': 30, 'count': 40}
        for bad in ([30, 30, 40], {'idle': 30}, {**good, 'extra': 1}, {**good, 'idle': True},
                    {**good, 'idle': 0}, {**good, 'interval': '30'}, {**good, 'count': 128}):
            self.data['keepalive'] = bad
            with self.assertRaises(ValueError, msg=bad): self.load()


class KeepaliveTests(unittest.TestCase):
    SETTINGS = {'idle': 30, 'interval': 30, 'count': 40}

    def test_sets_probe_schedule_and_matching_user_timeout(self):
        sock = Mock()
        a.make_keepalive(self.SETTINGS)(sock)
        self.assertEqual(sock.setsockopt.call_args_list, [
            call(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1),
            call(socket.IPPROTO_TCP, a.TCP_KEEPIDLE, 30),
            call(socket.IPPROTO_TCP, a.TCP_KEEPINTVL, 30),
            call(socket.IPPROTO_TCP, a.TCP_KEEPCNT, 40),
            call(socket.IPPROTO_TCP, a.TCP_USER_TIMEOUT, 1_230_000),
        ])

    def test_upstream_keepalive_replaced_only_when_configured(self):
        osutil = Mock()
        upstream = osutil.set_keepalive
        with patch.object(a.tunnel_service, 'OSUTIL', osutil):
            a.apply_keepalive({'keepalive': None})
            self.assertIs(osutil.set_keepalive, upstream)
            a.apply_keepalive({'keepalive': self.SETTINGS})
            sock = Mock()
            osutil.set_keepalive(sock)  # upstream passes only the socket
        upstream.assert_not_called()
        self.assertEqual(sock.setsockopt.call_count, 5)

    @unittest.skipUnless(sys.platform.startswith('linux'), 'Linux TCP options')
    def test_kernel_accepts_options(self):
        with socket.socket() as sock:
            a.make_keepalive(self.SETTINGS)(sock)
            self.assertEqual(sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT), 40)
            self.assertEqual(sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_USER_TIMEOUT), 1_230_000)


class DeviceTests(unittest.TestCase):
    def test_enables_ipv6_only_on_owned_interface(self):
        tun = Mock()
        with patch.object(a.tunnel_service, 'TunTapDevice', return_value=tun) as factory, \
             patch.object(a, 'Path') as path:
            self.assertIs(a.create_device('ignored'), tun)
        factory.assert_called_once_with(name='simloc-rsd')
        path.assert_called_once_with('/proc/sys/net/ipv6/conf/simloc-rsd/disable_ipv6')
        path.return_value.write_text.assert_called_once_with('0\n')
        tun.close.assert_not_called()

    def test_closes_interface_if_ipv6_configuration_fails(self):
        tun = Mock()
        with patch.object(a.tunnel_service, 'TunTapDevice', return_value=tun), \
             patch.object(a, 'Path') as path:
            path.return_value.write_text.side_effect = PermissionError()
            with self.assertRaises(PermissionError):
                a.create_device()
        tun.close.assert_called_once()


class SelfTestTests(unittest.IsolatedAsyncioTestCase):
    async def test_runs_tunnel_interface_steps_on_a_separate_name(self):
        tun = Mock()
        with patch.object(a.tunnel_service, 'TunTapDevice', return_value=tun) as device, \
             patch.object(a, 'Path') as path:
            await a.self_test()
        device.assert_called_once_with(name='simloc-test')
        path.assert_called_once_with('/proc/sys/net/ipv6/conf/simloc-test/disable_ipv6')
        self.assertEqual((tun.addr, tun.mtu), (a.SELF_TEST_ADDRESS, 1280))
        tun.up.assert_called_once()
        tun.close.assert_called_once()

    async def test_interface_is_removed_when_a_step_fails(self):
        tun = Mock()
        tun.up.side_effect = PermissionError()
        with patch.object(a.tunnel_service, 'TunTapDevice', return_value=tun), patch.object(a, 'Path'):
            with self.assertRaises(PermissionError):
                await a.self_test()
        tun.close.assert_called_once()


class MonitorTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_connection_closes_service_and_backs_off(self):
        service = AsyncMock()
        service.connect.side_effect = OSError()
        core = a.DirectCore({'udid': 'test'})
        with patch.object(a, 'DirectProvider', return_value=service), \
             patch.object(a.asyncio, 'sleep', side_effect=asyncio.CancelledError()) as sleep:
            with self.assertRaises(asyncio.CancelledError): await core.monitor_wifi_task()
        service.connect.assert_awaited_once_with(autopair=False)
        service.close.assert_awaited_once()
        sleep.assert_awaited_once_with(a.RETRY_MIN)
        self.assertEqual(core.tunnel_tasks, {})

    async def test_refused_port_falls_back_and_is_remembered(self):
        refused, answered = AsyncMock(), AsyncMock()
        refused.connect.side_effect = ConnectionRefusedError()
        tried = []
        def provider(config):
            tried.append(config['port'])
            return answered if config['port'] == 49154 else refused
        core = a.DirectCore({'udid': 'test', 'port': 49152})
        with patch.object(a, 'DirectProvider', side_effect=provider):
            self.assertIs(await core.connect_peer(), answered)
            self.assertEqual(tried, [49152, 49153, 49154])
            self.assertEqual(refused.close.await_count, 2)
            tried.clear()
            await core.connect_peer()
        self.assertEqual(tried, [49154], 'the port that answered goes first next time')

    async def test_timeout_does_not_scan_and_all_refused_reraises(self):
        slow = AsyncMock()
        slow.connect.side_effect = TimeoutError()
        with patch.object(a, 'DirectProvider', return_value=slow) as provider:
            with self.assertRaises(TimeoutError):
                await a.DirectCore({'udid': 'test', 'port': 49152}).connect_peer()
        provider.assert_called_once()
        slow.close.assert_awaited_once()
        refused = AsyncMock()
        refused.connect.side_effect = ConnectionRefusedError()
        with patch.object(a, 'DirectProvider', return_value=refused) as provider:
            with self.assertRaises(ConnectionRefusedError):
                await a.DirectCore({'udid': 'test', 'port': 50000}).connect_peer()
        self.assertEqual(provider.call_count, 1 + len(a.PORT_SCAN))
        self.assertEqual(refused.close.await_count, 1 + len(a.PORT_SCAN))

    async def test_cancellation_closes_unclaimed_service(self):
        service = AsyncMock()
        service.connect.side_effect = asyncio.CancelledError()
        with patch.object(a, 'DirectProvider', return_value=service):
            with self.assertRaises(asyncio.CancelledError):
                await a.DirectCore({'udid': 'test'}).monitor_wifi_task()
        service.close.assert_awaited_once()

    async def test_connected_service_registered_under_configured_device(self):
        service = AsyncMock()
        core = a.DirectCore({'udid': 'fixture-device'})
        observed = []
        async def tunnel_task(key, handler):
            observed.append((key, core.tunnel_tasks[key].udid, handler))
        with patch.object(a, 'DirectProvider', return_value=service), \
             patch.object(core, 'start_tunnel_task', side_effect=tunnel_task), \
             patch.object(a.asyncio, 'sleep', side_effect=asyncio.CancelledError()):
            with self.assertRaises(asyncio.CancelledError): await core.monitor_wifi_task()
        self.assertEqual(observed, [('wireguard-peer', 'fixture-device', service)])
        service.close.assert_not_awaited()  # Ownership was handed to upstream.

    async def test_shutdown_survives_upstream_swallowing_child_cancellation(self):
        service = AsyncMock()
        core = a.DirectCore({'udid': 'fixture-device'})
        ready = asyncio.Event()
        async def tunnel_task(key, handler):
            ready.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                pass
        with patch.object(a, 'DirectProvider', return_value=service), \
             patch.object(core, 'start_tunnel_task', side_effect=tunnel_task):
            monitor = asyncio.create_task(core.monitor_wifi_task())
            await ready.wait()
            monitor.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(monitor, .5)


if __name__ == '__main__': unittest.main()
