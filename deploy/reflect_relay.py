#!/usr/bin/env python3
"""Short-lived, mutually authenticated TLS relay for one reflected iPhone.

No pairing records, arbitrary destinations, shell execution, or file transfer.
Phone sockets always target 10.7.0.1:49152 and at most one explicitly granted
high data port. Server access is through a UID-checked private Unix socket.
"""
import argparse
import asyncio
import base64
import contextlib
import contextvars
import json
import re
import os
from pathlib import Path
import socket
import ssl
import struct

FRAME_LIMIT = 32768
CHUNK = 16384
MAX_CHANNELS = 4
MAX_SECONDS = 1800
PHONE_NODE = contextvars.ContextVar('phone_node', default='')


def report(*parts, **kwargs):
    node = PHONE_NODE.get()
    print(*([f'[{node}]'] if node else []), *parts, **kwargs)


def valid_port(port, data=False):
    return type(port) is int and ((49153 <= port <= 65535) if data else port == 49152)


async def receive(reader, timeout=None):
    line = await asyncio.wait_for(reader.readline(), timeout) if timeout else await reader.readline()
    if not line:
        raise EOFError()
    if len(line) > FRAME_LIMIT or not line.endswith(b'\n'):
        raise ValueError('invalid frame size')
    obj = json.loads(line)
    if not isinstance(obj, dict):
        raise ValueError('invalid frame type')
    return obj


async def send_json(writer, obj):
    data = json.dumps(obj, separators=(',', ':')).encode() + b'\n'
    if len(data) > FRAME_LIMIT:
        raise ValueError('outgoing frame too large')
    writer.write(data)
    await writer.drain()


class RelayError(ConnectionError):
    """A bounded error code that can safely cross the local control socket."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


def error_code(exc):
    if isinstance(exc, RelayError):
        return exc.code
    if isinstance(exc, ConnectionRefusedError):
        return 'phone_port_refused'
    if isinstance(exc, TimeoutError):
        return 'phone_port_timeout'
    return 'channel_closed'


class Channel:
    def __init__(self, reader, writer, port=None):
        self.reader, self.writer = reader, writer
        self.port = port
        self.ready = asyncio.Event()
        self.pump = None


class Mux:
    def __init__(self, reader, writer, phone=False):
        self.reader, self.writer, self.phone = reader, writer, phone
        self.allowed = {49152}
        self.channels = {}
        self.waiters = {}
        self.grant_waiter = None
        self.lock = asyncio.Lock()
        self.closed = asyncio.Event()
        self.next_id = 1
        self.opening = set()
        self.tasks = set()

    async def send(self, obj):
        async with self.lock:
            if self.closed.is_set():
                raise ConnectionError('relay closed')
            await send_json(self.writer, obj)

    async def pump(self, cid):
        channel = self.channels[cid]
        try:
            while data := await channel.reader.read(CHUNK):
                await self.send({'op': 'data', 'id': cid, 'data': base64.b64encode(data).decode()})
        except (ConnectionError, OSError):
            pass
        finally:
            with contextlib.suppress(ConnectionError, OSError):
                await self.send({'op': 'close', 'id': cid})
            self.drop(cid)

    def drop(self, cid):
        channel = self.channels.pop(cid, None)
        if channel:
            channel.ready.set()
            channel.writer.close()
            if channel.pump and channel.pump is not asyncio.current_task():
                channel.pump.cancel()
        waiter = self.waiters.pop(cid, None)
        if waiter and not waiter.done():
            waiter.set_exception(ConnectionError('channel closed'))

    def track(self, task):
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    async def dial_target(self, port):
        return await asyncio.open_connection('10.7.0.1', port)

    async def phone_open(self, cid, port):
        writer = None
        try:
            reader, writer = await asyncio.wait_for(self.dial_target(port), 5)
            if self.closed.is_set():
                return
            channel = Channel(reader, writer, port)
            channel.ready.set()
            self.channels[cid] = channel
            await self.send({'op': 'opened', 'id': cid, 'ok': True})
            channel.pump = self.track(asyncio.create_task(self.pump(cid)))
            writer = None  # Ownership transferred to Channel.
            report('PHONE_SOCKET_OPEN', port, flush=True)
        except (ConnectionError, OSError, asyncio.TimeoutError) as exc:
            report('PHONE_SOCKET_FAILED', port, type(exc).__name__, flush=True)
            with contextlib.suppress(ConnectionError, OSError):
                await self.send({'op': 'opened', 'id': cid, 'ok': False,
                                 'error': error_code(exc)})
        finally:
            self.opening.discard(cid)
            if writer:
                writer.close()

    def can_grant(self, port):
        # Replace a stale grant only after every previous data stream has closed.
        # The control stream can stay open while negotiating the next tunnel.
        return valid_port(port, data=True) and (
            port in self.allowed or
            (not self.opening and all(c.port == 49152 for c in self.channels.values())))

    async def grant(self, port):
        if not self.can_grant(port):
            raise ValueError('previous data channel must close before a new grant')
        if self.grant_waiter:
            raise ValueError('grant already in progress')
        future = asyncio.get_running_loop().create_future()
        self.grant_waiter = (port, future)
        try:
            await self.send({'op': 'grant', 'port': port})
            await asyncio.wait_for(future, 10)
            self.allowed = {49152, port}
        finally:
            self.grant_waiter = None

    async def open_local(self, port, reader, writer):
        if type(port) is not int or port not in self.allowed or len(self.channels) >= MAX_CHANNELS:
            raise ValueError('port denied or channel limit reached')
        cid = self.next_id
        self.next_id += 1
        channel = Channel(reader, writer, port)
        self.channels[cid] = channel
        future = asyncio.get_running_loop().create_future()
        self.waiters[cid] = future
        try:
            try:
                await self.send({'op': 'open', 'id': cid, 'port': port})
                await asyncio.wait_for(future, 10)
            except (ConnectionError, OSError, TimeoutError) as exc:
                # Reply before drop() closes the Unix socket. Previously EOF hid
                # phone dial failures behind JSONDecodeError in the caller.
                code = error_code(exc)
                report('CHANNEL_OPEN_FAILED', port, code, flush=True)
                await send_json(writer, {'ok': False, 'error': code})
                return
            await send_json(writer, {'ok': True})
            channel.ready.set()
            channel.pump = self.track(asyncio.create_task(self.pump(cid)))
            await channel.pump
        finally:
            with contextlib.suppress(ConnectionError, OSError):
                await self.send({'op': 'close', 'id': cid})
            self.drop(cid)

    async def dispatch(self, frame):
        op = frame.get('op')
        if op == 'ping':
            await self.send({'op': 'pong'})
            return
        if op == 'pong':
            return
        if op == 'grant' and self.phone:
            port = frame.get('port')
            if not self.can_grant(port):
                raise ValueError('invalid grant or previous data channel still open')
            self.allowed = {49152, port}
            await self.send({'op': 'granted', 'port': port})
            return
        if op == 'granted' and not self.phone:
            if not self.grant_waiter or frame.get('port') != self.grant_waiter[0]:
                raise ValueError('unexpected grant response')
            future = self.grant_waiter[1]
            if not future.done():
                future.set_result(True)
            return
        cid = frame.get('id')
        if type(cid) is not int or not 1 <= cid <= 2147483647:
            raise ValueError('invalid channel')
        if op == 'open' and self.phone:
            port = frame.get('port')
            if (type(port) is not int or port not in self.allowed or
                    cid in self.channels or cid in self.opening or
                    len(self.channels) + len(self.opening) >= MAX_CHANNELS):
                raise ValueError('unauthorized open')
            self.opening.add(cid)
            task = asyncio.create_task(self.phone_open(cid, port))
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)
        elif op == 'opened' and not self.phone:
            future = self.waiters.pop(cid, None)
            if future and not future.done():
                if frame.get('ok') is True:
                    future.set_result(True)
                else:
                    code = frame.get('error')
                    if code not in {'phone_port_refused', 'phone_port_timeout', 'channel_closed'}:
                        code = 'phone_dial_failed'
                    future.set_exception(RelayError(code))
        elif op == 'data':
            data = frame.get('data')
            if not isinstance(data, str) or len(data) > 4 * ((CHUNK + 2) // 3):
                raise ValueError('invalid payload size')
            raw = base64.b64decode(data, validate=True)
            if len(raw) > CHUNK:
                raise ValueError('oversized payload')
            channel = self.channels.get(cid)
            if channel:
                await channel.ready.wait()
                if cid in self.channels:
                    try:
                        channel.writer.write(raw)
                        await channel.writer.drain()
                    except (ConnectionError, OSError):
                        self.drop(cid)
        elif op == 'close':
            self.drop(cid)
        else:
            raise ValueError('unknown operation')

    async def heartbeat(self):
        while True:
            await asyncio.sleep(15)
            await self.send({'op': 'ping'})

    async def run(self):
        heartbeat = asyncio.create_task(self.heartbeat())
        try:
            while True:
                await self.dispatch(await receive(self.reader, 60))
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
            self.closed.set()
            for cid in list(self.channels):
                self.drop(cid)
            for task in list(self.tasks):
                task.cancel()
            await asyncio.gather(*self.tasks, return_exceptions=True)
            if self.grant_waiter and not self.grant_waiter[1].done():
                self.grant_waiter[1].set_exception(ConnectionError('relay closed'))
            self.writer.close()
            with contextlib.suppress(Exception):
                await self.writer.wait_closed()


async def run_phone_once(config):
    context = ssl.create_default_context(cafile=config['ca'])
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    context.load_cert_chain(config['certificate'], config['private_key'])
    reader, writer = await asyncio.wait_for(asyncio.open_connection(
        config['server'], config['port'], ssl=context,
        server_hostname='reflect-relay.invalid', limit=FRAME_LIMIT,
        ssl_handshake_timeout=10), 20)
    try:
        await send_json(writer, {'op': 'hello', 'version': 1})
        reply = await receive(reader, 10)
        if reply != {'ok': True, 'version': 1}:
            raise RelayError('server_busy_or_rejected')
        report('RELAY_READY: transport only; keep a-Shell foreground; establish device channel on Wi-Fi', flush=True)
        await Mux(reader, writer, phone=True).run()
    finally:
        writer.close()
        with contextlib.suppress(ConnectionError, OSError):
            await writer.wait_closed()
        report('RELAY_ENDED: server transport closed', flush=True)


async def run_phone(config):
    deadline = asyncio.timeout(config.get('max_seconds', MAX_SECONDS))
    try:
        async with deadline:
            while True:
                try:
                    await run_phone_once(config)
                except ssl.SSLCertVerificationError:
                    raise  # A certificate failure needs a renewed/trusted package.
                except (EOFError, ConnectionError, OSError, TimeoutError) as exc:
                    if not config.get('reconnect', False):
                        raise
                    report('RECONNECTING', error_code(exc), 'retry_in=5s', flush=True)
                if not config.get('reconnect', False):
                    return
                await asyncio.sleep(5)
    except TimeoutError:
        if not deadline.expired():
            raise
        report('SESSION_LIMIT_REACHED: restart explicitly to begin another session', flush=True)


async def run_phone_fleet(config):
    """Independent transports: one failed host never cancels the other hosts."""
    async def one(node):
        token = PHONE_NODE.set(node['name'])
        try:
            await run_phone({**node, 'max_seconds': config.get('max_seconds', MAX_SECONDS),
                             'reconnect': config.get('reconnect', True)})
            return True
        except (OSError, EOFError, ValueError) as exc:
            report('NODE_STOPPED', exc.code if isinstance(exc, RelayError) else type(exc).__name__, flush=True)
            return False
        finally:
            PHONE_NODE.reset(token)

    results = await asyncio.gather(*(one(node) for node in config['servers']))
    if not any(results):
        raise RelayError('all_nodes_stopped')


class RelayServer:
    def __init__(self, probe_uid):
        self.probe_uid = probe_uid
        self.session = None

    async def phone(self, reader, writer):
        mux = None
        try:
            request = await receive(reader, 5)
            if request != {'op': 'hello', 'version': 1} or self.session:
                await send_json(writer, {'ok': False})
                return
            mux = Mux(reader, writer)
            self.session = mux
            await send_json(writer, {'ok': True, 'version': 1})
            report('PHONE_CONNECTED', flush=True)
            await mux.run()
        except (EOFError, ValueError, ConnectionError, OSError, asyncio.TimeoutError) as exc:
            report('PHONE_SESSION_ENDED', type(exc).__name__, flush=True)
        finally:
            if self.session is mux:
                self.session = None
            writer.close()

    async def local(self, reader, writer):
        try:
            sock = writer.get_extra_info('socket')
            _, uid, _ = struct.unpack('3i', sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            if uid not in {0, self.probe_uid}:
                raise PermissionError('local uid denied')
            request = await receive(reader, 5)
            mux = self.session
            connected = mux is not None and not mux.closed.is_set()
            if request.get('op') == 'status':
                await send_json(writer, {'ok': True, 'phone_connected': connected})
                return
            if not connected:
                raise RelayError('phone_not_connected')
            if request.get('op') == 'grant':
                await mux.grant(request.get('port'))
                await send_json(writer, {'ok': True})
            elif request.get('op') == 'open':
                await mux.open_local(request.get('port'), reader, writer)
            else:
                raise ValueError('invalid local operation')
        except (EOFError, ValueError, ConnectionError, OSError, asyncio.TimeoutError) as exc:
            with contextlib.suppress(ConnectionError, OSError):
                await send_json(writer, {'ok': False, 'error': error_code(exc)})
        finally:
            writer.close()


async def run_server(config):
    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH, cafile=config['ca'])
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    context.verify_mode = ssl.CERT_REQUIRED
    context.load_cert_chain(config['certificate'], config['private_key'])
    relay = RelayServer(config['probe_uid'])
    path = Path(config['socket'])
    if path.exists():
        raise FileExistsError('refusing to replace an existing local socket')
    local = await asyncio.start_unix_server(relay.local, path=str(path), limit=FRAME_LIMIT)
    os.chmod(path, 0o660)
    try:
        public = await asyncio.start_server(relay.phone, '0.0.0.0', config['port'],
            ssl=context, ssl_handshake_timeout=5, limit=FRAME_LIMIT, backlog=8)
        report('LISTENING_WITH_CLIENT_CERT_REQUIRED', flush=True)
        async with local, public:
            await asyncio.wait_for(asyncio.gather(local.serve_forever(), public.serve_forever()),
                                   config.get('max_seconds', MAX_SECONDS))
    finally:
        local.close()
        await local.wait_closed()
        path.unlink(missing_ok=True)


def load_config(path):
    path = Path(path).resolve()
    config = json.loads(path.read_text())
    if not isinstance(config, dict):
        raise ValueError('config must be an object')
    duration = config.get('max_seconds', MAX_SECONDS)
    # null means no limit: a persistent server session, or a phone left running on purpose.
    if duration is not None and (type(duration) is not int or not 60 <= duration <= 86400):
        raise ValueError('max_seconds must be null or between 60 and 86400')
    if type(config.get('reconnect', False)) is not bool:
        raise ValueError('reconnect must be a boolean')
    nodes = config.get('servers', [config])
    if not isinstance(nodes, list) or not 1 <= len(nodes) <= 16:
        raise ValueError('servers must contain 1..16 nodes')
    names = set()
    for node in nodes:
        if not isinstance(node, dict):
            raise ValueError('node must be an object')
        if 'servers' in config:
            name = node.get('name', '')
            if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', name) or name in names:
                raise ValueError('node names must be unique safe identifiers')
            names.add(name)
            if not isinstance(node.get('server'), str) or not node['server'].strip():
                raise ValueError('node needs a server address')
            if type(node.get('port')) is not int or not 1 <= node['port'] <= 65535:
                raise ValueError('invalid node port')
        for key in ('ca', 'certificate', 'private_key'):
            resolved = (path.parent / node[key]).resolve()
            if 'servers' in config and not resolved.is_relative_to(path.parent):
                raise ValueError('fleet credential paths must stay inside the package')
            node[key] = str(resolved)
    return config


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('role', choices=['phone', 'server'])
    parser.add_argument('--config', required=True)
    parser.add_argument('--reconnect', action='store_true', help='retry phone transport disconnects')
    parser.add_argument('--max-seconds', type=int, help='session duration, 60..86400 seconds')
    parser.add_argument('--nodes', nargs='+', help='connect only these named fleet nodes')
    args = parser.parse_args()
    if args.reconnect and args.role != 'phone':
        parser.error('--reconnect is only valid for the phone role')
    if args.max_seconds is not None and not 60 <= args.max_seconds <= 86400:
        parser.error('--max-seconds must be between 60 and 86400')
    os.umask(0o077)
    try:
        config = load_config(args.config)
        if args.reconnect:
            config['reconnect'] = True
        if args.max_seconds is not None:
            config['max_seconds'] = args.max_seconds
        if 'servers' in config:
            if args.role != 'phone':
                parser.error('fleet configuration is only supported for the phone role')
            if args.nodes:
                unknown = set(args.nodes) - {n['name'] for n in config['servers']}
                if unknown:
                    parser.error('unknown node name')
                config['servers'] = [n for n in config['servers'] if n['name'] in args.nodes]
            asyncio.run(run_phone_fleet(config))
        else:
            if args.nodes:
                parser.error('--nodes requires a fleet phone configuration')
            asyncio.run((run_phone if args.role == 'phone' else run_server)(config))
    except KeyboardInterrupt:
        report('STOPPED', flush=True)
    except Exception as exc:
        report('STOPPED', exc.code if isinstance(exc, RelayError) else type(exc).__name__, flush=True)
        raise SystemExit(1)


if __name__ == '__main__':
    main()
