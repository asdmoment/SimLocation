#!/usr/bin/env python3
"""Hold one RSD/DVT/LocationSimulation session through phone sleep; never sends coordinates.

Checks whether the *same* DVT connection still answers a read-only request after the
phone has been locked on cellular, and records the phone's WireGuard activity passively.
SIGUSR1 runs a check; a final check runs at the deadline; SIGTERM stops without one.
Output is redacted JSON lines on stdout and in LOG_PATH (mode 600).
"""
import argparse
import asyncio
import json
import os
import signal
import subprocess
import time
import urllib.request
from contextlib import AsyncExitStack
from pathlib import Path

import pymobiledevice3.service_connection as service_connection
from pymobiledevice3.remote.remote_service_discovery import RemoteServiceDiscoveryService
from pymobiledevice3.services.dvt.instruments.device_info import DeviceInfo
from pymobiledevice3.services.dvt.instruments.dvt_provider import DvtProvider
from pymobiledevice3.services.dvt.instruments.location_simulation import LocationSimulation

from network_tunneld import load_config, make_keepalive

CONFIG = '/etc/simlocation/network.json'
LOG_PATH = Path('/var/lib/simlocation-tunnel/hold-test.jsonl')
WG_INTERFACE = 'wg-simloc'
SETUP_TIMEOUT = 30
CHECK_TIMEOUT = 60
WG_POLL_SECONDS = 5
WG_SUMMARY_SECONDS = 60


class Recorder:
    def __init__(self, path):
        self.started = time.monotonic()
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        self.out = os.fdopen(fd, 'w')

    def __call__(self, kind, **fields):
        row = {'utc': time.strftime('%H:%M:%S', time.gmtime()),
               'elapsed': round(time.monotonic() - self.started, 1), 'kind': kind, **fields}
        line = json.dumps(row)
        self.out.write(line + '\n')
        self.out.flush()
        print(line, flush=True)

    def close(self):
        self.out.close()


def redact_endpoint(endpoint):
    """Keep the carrier-identifying /16 and the NAT port; drop the host part."""
    if endpoint in ('', '(none)'):
        return '(none)'
    host, sep, port = endpoint.rpartition(':')
    parts = host.split('.')
    if not sep or len(parts) != 4:
        return '<redacted>'
    return f'{parts[0]}.{parts[1]}.x.x:{port}'


def parse_wg(handshakes, transfer, endpoints):
    """Single-peer `wg show` field output -> counters. Peer keys are dropped here."""
    def values(text):
        rows = [line.split('\t') for line in text.strip().splitlines() if line.strip()]
        if len(rows) != 1:
            raise ValueError('expected exactly one WireGuard peer')
        return rows[0][1:]
    (handshake,), (rx, tx), (endpoint,) = values(handshakes), values(transfer), values(endpoints)
    return {'handshake': int(handshake), 'rx': int(rx), 'tx': int(tx),
            'endpoint': redact_endpoint(endpoint)}


def sample_wg():
    # Never `wg show dump`: its first line carries the interface private key.
    outputs = [subprocess.run(['wg', 'show', WG_INTERFACE, field], capture_output=True,
                              text=True, timeout=5, check=True).stdout
               for field in ('latest-handshakes', 'transfer', 'endpoints')]
    return parse_wg(*outputs)


def registered_tunnel(udid):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open('http://127.0.0.1:49151/', timeout=5) as response:
        entries = json.load(response).get(udid, [])
    return entries[0] if entries else None


async def watch_wireguard(record, stop):
    """Log when the phone sends anything, re-handshakes or roams; summarize every minute."""
    last = None
    last_at = 0.0
    while not stop.is_set():
        try:
            now = await asyncio.to_thread(sample_wg)
        except Exception as exc:
            record('wg_error', error=type(exc).__name__)
        else:
            changed = last is None or any(now[k] != last[k] for k in ('rx', 'handshake', 'endpoint'))
            if changed or time.monotonic() - last_at >= WG_SUMMARY_SECONDS:
                record('wg',
                       rx_delta=None if last is None else now['rx'] - last['rx'],
                       tx_delta=None if last is None else now['tx'] - last['tx'],
                       handshake_age=int(time.time()) - now['handshake'] if now['handshake'] else None,
                       endpoint=now['endpoint'],
                       roamed=last is not None and now['endpoint'] != last['endpoint'])
                last, last_at = now, time.monotonic()
        try:
            await asyncio.wait_for(stop.wait(), WG_POLL_SECONDS)
        except TimeoutError:
            pass


async def check(dvt, record, reason, tunnel_key, udid):
    """Open a fresh read-only channel on the held DVT connection: 'ok', 'timeout' or 'dead'."""
    try:
        entry = await asyncio.to_thread(registered_tunnel, udid)
        same_tunnel = entry is not None and (entry['tunnel-address'], entry['tunnel-port']) == tunnel_key
    except Exception:
        same_tunnel = None
    started = time.monotonic()
    try:
        async with asyncio.timeout(CHECK_TIMEOUT):
            async with DeviceInfo(dvt) as info:
                await info.mach_time_info()
        outcome, error = 'ok', None
    except TimeoutError:
        outcome, error = 'timeout', 'TimeoutError'
    except Exception as exc:
        outcome, error = 'dead', type(exc).__name__
    record('check', reason=reason, outcome=outcome, error=error,
           seconds=round(time.monotonic() - started, 1), same_tunnel=same_tunnel)
    return outcome


async def next_reason(checks, stop, loop, deadline):
    remaining = deadline - loop.time()
    if remaining <= 0:
        return 'deadline'
    waiters = {asyncio.create_task(checks.get()), asyncio.create_task(stop.wait())}
    done, pending = await asyncio.wait(waiters, timeout=remaining, return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        task.cancel()
    if stop.is_set():
        return None
    for task in done:
        return task.result()
    return 'deadline'


async def run(duration, record):
    config = load_config(CONFIG)
    if config['keepalive']:
        # Inner service connections otherwise inherit upstream's ~12 s dead-peer timeout.
        service_connection.OSUTIL.set_keepalive = make_keepalive(config['keepalive'])
    record('start', keepalive=config['keepalive'], duration=duration)
    entry = await asyncio.to_thread(registered_tunnel, config['udid'])
    if entry is None:
        record('abort', reason='no registered tunnel')
        return 2
    key = (entry['tunnel-address'], entry['tunnel-port'])
    loop = asyncio.get_running_loop()
    checks, stop = asyncio.Queue(), asyncio.Event()
    loop.add_signal_handler(signal.SIGUSR1, checks.put_nowait, 'signal')
    loop.add_signal_handler(signal.SIGTERM, stop.set)
    watcher = asyncio.create_task(watch_wireguard(record, stop))
    outcome = 'dead'
    try:
        async with AsyncExitStack() as stack:
            async with asyncio.timeout(SETUP_TIMEOUT):
                rsd = await stack.enter_async_context(RemoteServiceDiscoveryService(key))
                dvt = await stack.enter_async_context(DvtProvider(rsd))
                await stack.enter_async_context(LocationSimulation(dvt))
            record('hold_open', note='LocationSimulation channel open; no coordinates sent')
            deadline = loop.time() + duration
            reason = 'initial'
            while reason is not None:
                outcome = await check(dvt, record, reason, key, config['udid'])
                if outcome == 'dead' or reason == 'deadline':
                    break
                reason = await next_reason(checks, stop, loop, deadline)
    except Exception as exc:
        record('session_error', error=type(exc).__name__)
    finally:
        stop.set()
        await watcher
        record('end', last_outcome=outcome)
    return 0 if outcome == 'ok' else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--duration', type=int, default=3600, help='seconds to hold (60-14400)')
    args = parser.parse_args()
    if not 60 <= args.duration <= 14400:
        raise SystemExit('--duration must be between 60 and 14400 seconds')
    os.umask(0o077)
    record = Recorder(LOG_PATH)
    try:
        raise SystemExit(asyncio.run(run(args.duration, record)))
    finally:
        record.close()


if __name__ == '__main__':
    main()
