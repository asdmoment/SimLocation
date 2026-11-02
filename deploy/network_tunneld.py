#!/usr/bin/env python3
"""Pinned pymobiledevice3 11.49.2 adapter for one explicit RemotePairing peer.

The upstream tunneld HTTP API and tunnel lifecycle are reused unchanged. Only
Bonjour discovery is replaced with a fixed address. Never initiates new pairing.
"""
import argparse
import asyncio
import ipaddress
import json
import logging
import re
import socket
import time
from contextlib import AsyncExitStack, asynccontextmanager, suppress
from functools import partial
from importlib.metadata import version
from pathlib import Path

import pymobiledevice3.remote.tunnel_service as tunnel_service
from pymobiledevice3.remote.common import TunnelProtocol
from pymobiledevice3.remote.tunnel_service import RemotePairingTunnelService
from pymobiledevice3.tunneld.server import TunnelTask, TunneldCore, TunneldRunner

PINNED_VERSION = '11.49.2'
CONNECT_TIMEOUT = 12
SETUP_TIMEOUT = 25
RETRY_MIN = 5
RETRY_MAX = 30
# Where iOS 27 puts the RemotePairing listener (tailnet-tunnel.py scans the same range).
PORT_SCAN = range(49152, 49168)
LOG = logging.getLogger('simlocation.network')
TUN_INTERFACE = 'simloc-rsd'
# Linux values; the adapter only runs on Linux, the fallbacks keep tests importable elsewhere.
TCP_KEEPIDLE = getattr(socket, 'TCP_KEEPIDLE', 4)
TCP_KEEPINTVL = getattr(socket, 'TCP_KEEPINTVL', 5)
TCP_KEEPCNT = getattr(socket, 'TCP_KEEPCNT', 6)
TCP_USER_TIMEOUT = getattr(socket, 'TCP_USER_TIMEOUT', 18)
KEEPALIVE_LIMITS = {'idle': (1, 3600), 'interval': (1, 600), 'count': (1, 127)}


def create_device(_interface_name=None):
    """Enable IPv6 only on our new link, even on IPv4-only VPS hosts."""
    tun = tunnel_service.TunTapDevice(name=TUN_INTERFACE)
    try:
        Path(f'/proc/sys/net/ipv6/conf/{TUN_INTERFACE}/disable_ipv6').write_text('0\n')
    except BaseException:
        tun.close()
        raise
    return tun


def load_config(path):
    data = json.loads(Path(path).read_text())
    if not isinstance(data, dict):
        raise ValueError('Configuration must be an object')
    if not re.fullmatch(r'[a-fA-F0-9-]{16,64}', data.get('udid', '')):
        raise ValueError('Invalid device identifier')
    ipaddress.ip_address(data['host'])
    port = data.get('port', 49152)
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError('Invalid RemotePairing port')
    record = Path(data['pair_record'])
    if not record.is_absolute() or not record.is_file():
        raise ValueError('Pair record must be an existing absolute file')
    if record.stat().st_mode & 0o077:
        raise ValueError('Pair record must be private (mode 600)')
    keepalive = data.get('keepalive')
    if keepalive is not None:
        if not isinstance(keepalive, dict) or set(keepalive) != set(KEEPALIVE_LIMITS):
            raise ValueError('keepalive needs exactly idle, interval and count')
        for name, (low, high) in KEEPALIVE_LIMITS.items():
            value = keepalive[name]
            if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
                raise ValueError(f'Invalid keepalive {name}')
    return {**data, 'port': port, 'pair_record': record, 'keepalive': keepalive}


def make_keepalive(settings):
    """Tolerate a silent peer for idle + interval * count seconds.

    Upstream probes after 3 s idle and gives up after three 3 s probes. On cellular a
    locked phone or a radio gap outlasts that, and a dropped tunnel cannot be rebuilt
    until the phone is on a non-cellular path again.
    """
    idle, interval, count = settings['idle'], settings['interval'], settings['count']
    user_timeout_ms = (idle + interval * count) * 1000

    def set_keepalive(sock, *_args, **_kwargs):
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        sock.setsockopt(socket.IPPROTO_TCP, TCP_KEEPIDLE, idle)
        sock.setsockopt(socket.IPPROTO_TCP, TCP_KEEPINTVL, interval)
        sock.setsockopt(socket.IPPROTO_TCP, TCP_KEEPCNT, count)
        # Unacknowledged data would otherwise fall back to tcp_retries2 (~15 min).
        sock.setsockopt(socket.IPPROTO_TCP, TCP_USER_TIMEOUT, user_timeout_ms)
    return set_keepalive


def apply_keepalive(config):
    settings = config.get('keepalive')
    if settings:
        tunnel_service.OSUTIL.set_keepalive = make_keepalive(settings)
        LOG.info('Tunnel keepalive: idle %ss, interval %ss, count %s',
                 settings['idle'], settings['interval'], settings['count'])


class DirectProvider(RemotePairingTunnelService):
    def __init__(self, config):
        self.record_file = config['pair_record']
        super().__init__(config['udid'], config['host'], config['port'])

    @property
    def pair_record_path(self):
        return self.record_file

    @asynccontextmanager
    async def start_tcp_tunnel(self):
        async with AsyncExitStack() as stack:
            # Bound only setup, not the lifetime of an established tunnel.
            try:
                async with asyncio.timeout(SETUP_TIMEOUT):
                    result = await stack.enter_async_context(super().start_tcp_tunnel())
            except Exception as exc:
                LOG.warning('Data tunnel setup failed (%s, errno=%s)',
                            type(exc).__name__, getattr(exc, 'errno', None))
                raise
            yield result


class DirectCore(TunneldCore):
    def __init__(self, config):
        super().__init__(protocol=TunnelProtocol.TCP, wifi_monitor=True,
                         usb_monitor=False, usbmux_monitor=False, mobdev2_monitor=False)
        self.config = config
        self.port = config.get('port', PORT_SCAN[0])

    async def connect_peer(self):
        """Verify trust on the last port that answered, then on the rest of PORT_SCAN.

        Only a refusal moves on: the listener may come back on another port after a reboot.
        A timeout means the phone is not reachable at all, and scanning would only multiply
        the wait. On cellular every port refuses, which the caller logs as before.
        """
        ports = [self.port, *(port for port in PORT_SCAN if port != self.port)]
        for index, port in enumerate(ports):
            service = DirectProvider({**self.config, 'port': port})
            try:
                async with asyncio.timeout(CONNECT_TIMEOUT):
                    await service.connect(autopair=False)
            except ConnectionRefusedError:
                with suppress(Exception):
                    await service.close()
                if index == len(ports) - 1:
                    raise
                continue
            except BaseException:
                with suppress(Exception):
                    await service.close()
                raise
            if port != self.port:
                LOG.info('RemotePairing answered on port %s', port)
                self.port = port
            return service

    async def monitor_wifi_task(self):
        delay = RETRY_MIN
        key = 'wireguard-peer'
        while True:
            service = None
            claimed = False
            started = time.monotonic()
            try:
                service = await self.connect_peer()
                LOG.info('Existing RemotePairing trust verified')
                task = asyncio.create_task(self.start_tunnel_task(key, service),
                                           name='wireguard-device-tunnel')
                self.tunnel_tasks[key] = TunnelTask(task=task, udid=self.config['udid'])
                claimed = True
                await task
                # Upstream swallows child cancellation during normal tunnel cleanup.
                if asyncio.current_task().cancelling():
                    raise asyncio.CancelledError
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Never log trust records, handshake payloads, or private keys.
                LOG.warning('Peer unavailable (%s); retry in %ss', type(exc).__name__, delay)
            finally:
                if service is not None and not claimed:
                    with suppress(Exception):
                        await service.close()
            if time.monotonic() - started > 60:
                delay = RETRY_MIN
            await asyncio.sleep(delay)
            delay = min(delay * 2, RETRY_MAX)


def make_runner(config):
    runner = TunneldRunner('127.0.0.1', 49151, protocol=TunnelProtocol.TCP,
                          wifi_monitor=False, usb_monitor=False,
                          usbmux_monitor=False, mobdev2_monitor=False)
    runner._tunneld_core = DirectCore(config)
    return runner


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    args = parser.parse_args()
    if version('pymobiledevice3') != PINNED_VERSION:
        raise SystemExit('Adapter requires pymobiledevice3 ' + PINNED_VERSION)
    config = load_config(args.config)
    logging.basicConfig(level=logging.INFO)
    apply_keepalive(config)
    # The kernel tunnel is shared by the web process and its background children.
    tunnel_service.USE_USERSPACE_TUNNEL = False
    tunnel_service.create_tun_device = create_device
    # Upstream's synchronous data-socket connect otherwise has no time limit.
    tunnel_service.create_connection = partial(socket.create_connection, timeout=10)
    make_runner(config)._run_app()


if __name__ == '__main__':
    main()
