#!/usr/bin/env python3
"""Expose the existing tunneld API through an on-phone, authenticated relay.

Alternative to network_tunneld.py, not an additional daemon on port 49151.
Pairing keys stay on this host. No pairing, DDI mount or location writes.
"""
import argparse
import asyncio
from contextlib import asynccontextmanager, suppress
from functools import partial
from importlib.metadata import version
import logging
from pathlib import Path
import ssl

import network_tunneld as network
from reflect_relay import RelayError, receive, send_json, valid_port
from pymobiledevice3.remote.common import TunnelProtocol
from pymobiledevice3.remote.tunnel_service import RemotePairingTcpTunnel, TunnelResult

LOG = logging.getLogger('simlocation.reflect')
DEFAULT_SOCKET = '/run/simloc-reflect/relay.sock'


async def relay_request(path, op, port=None):
    reader, writer = await asyncio.open_unix_connection(path)
    try:
        request = {'op': op}
        if port is not None:
            request['port'] = port
        await send_json(writer, request)
        try:
            reply = await receive(reader, 15)
        except (EOFError, ValueError) as exc:
            raise RelayError('relay_closed_before_reply') from exc
        if reply.get('ok') is not True:
            code = reply.get('error')
            if code not in {'phone_port_refused', 'phone_port_timeout', 'phone_dial_failed',
                            'phone_not_connected', 'channel_closed'}:
                code = 'relay_request_rejected'
            raise RelayError(code)
        return reader, writer
    except BaseException:
        writer.close()
        with suppress(OSError):
            await writer.wait_closed()
        raise


class ReflectedProvider(network.DirectProvider):
    def __init__(self, config):
        super().__init__(config)
        self.relay_socket = config.get('relay_socket', DEFAULT_SOCKET)

    async def connect(self, autopair=False):
        if autopair:
            raise ValueError('New pairing is disabled for relay connections')
        self._reader, self._writer = await relay_request(self.relay_socket, 'open', 49152)
        try:
            await self._attempt_pair_verify()
            if not await self._validate_pairing():
                raise RelayError('existing_identity_rejected')
            self._init_client_server_main_encryption_keys()
        except BaseException:
            with suppress(Exception):
                await self.close()
            raise

    @asynccontextmanager
    async def start_tcp_tunnel(self):
        tunnel = None
        writer = None
        try:
            async with asyncio.timeout(45):
                parameters = await self.create_tcp_listener()
                port = parameters['port']
                if not valid_port(port, data=True):
                    raise ValueError('Device data port is outside the allowed range')
                _, grant_writer = await relay_request(self.relay_socket, 'grant', port)
                grant_writer.close()
                await grant_writer.wait_closed()
                reader, writer = await relay_request(self.relay_socket, 'open', port)
                ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
                ctx.minimum_version = ssl.TLSVersion.TLSv1_2
                ctx.maximum_version = ssl.TLSVersion.TLSv1_2
                # The inner transport authenticates with the verified pairing PSK;
                # the outer transport independently requires verified mTLS.
                self._set_psk_ciphers(ctx)
                ctx.set_psk_client_callback(lambda hint: (None, self.encryption_key))
                await writer.start_tls(ctx, server_hostname='')
                tunnel = RemotePairingTcpTunnel(reader, writer)
                handshake = await tunnel.request_tunnel_establish()
                await tunnel.start_tunnel(handshake['clientParameters']['address'],
                                          handshake['clientParameters']['mtu'])
            result = TunnelResult(tunnel.tun.name, handshake['serverAddress'],
                                  handshake['serverRSDPort'], TunnelProtocol.TCP,
                                  tunnel, self.auxiliary_metadata)
            LOG.info('DEVICE_TUNNEL_READY: RSD published; no location requested')
            yield result
        except Exception as exc:
            LOG.warning('DEVICE_TUNNEL_FAILED: %s',
                        exc.code if isinstance(exc, RelayError) else type(exc).__name__)
            raise
        finally:
            if tunnel is not None:
                with suppress(Exception):
                    await tunnel.stop_tunnel()
            if writer is not None:
                writer.close()
                with suppress(OSError):
                    await writer.wait_closed()


class ReflectedCore(network.DirectCore):
    async def connect_peer(self):
        service = ReflectedProvider(self.config)
        try:
            async with asyncio.timeout(30):
                await service.connect(autopair=False)
            return service
        except BaseException as exc:
            with suppress(Exception):
                await service.close()
            if isinstance(exc, RelayError):
                LOG.warning('DEVICE_WAITING: %s (transport online is not device ready)', exc.code)
            raise


def make_runner(config):
    runner = network.make_runner(config)
    runner._tunneld_core = ReflectedCore(config)
    return runner


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('--relay-socket', default=DEFAULT_SOCKET)
    args = parser.parse_args()
    if version('pymobiledevice3') != network.PINNED_VERSION:
        parser.error('Requires pymobiledevice3 ' + network.PINNED_VERSION)
    if not getattr(ssl, 'HAS_PSK', False):
        parser.error('Requires Python >= 3.13 with TLS-PSK support')
    if not Path(args.relay_socket).is_absolute():
        parser.error('--relay-socket must be absolute')
    config = {**network.load_config(args.config), 'relay_socket': args.relay_socket}
    logging.basicConfig(level=logging.INFO)
    network.tunnel_service.USE_USERSPACE_TUNNEL = False
    network.tunnel_service.create_tun_device = network.create_device
    # Keep the proven cache fix in this backend process. Existing identity files
    # remain at the explicitly configured location, never copied onto the phone.
    import pymobiledevice3.remote.remote_service_discovery as rsd_module
    rsd_module.create_using_remote = partial(
        rsd_module.create_using_remote, autopair=False,
        pairing_records_cache_folder=Path('/run/simloc-reflect-backend/pair-cache'))
    make_runner(config)._run_app()


if __name__ == '__main__':
    main()
