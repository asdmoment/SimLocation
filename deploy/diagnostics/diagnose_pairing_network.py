"""Bounded, single-process network diagnostics; never changes location."""
import asyncio, json, os, re, time, urllib.request
from pathlib import Path
from pymobiledevice3.remote.remote_service_discovery import RemoteServiceDiscoveryService
from pymobiledevice3.services.os_trace import OsTraceService

LOG_PATH = Path('/var/lib/simlocation-tunnel/network-diagnostic.jsonl')
NAMES = {'remotepairingdeviced', 'remotepairingd'}
INTEREST = re.compile(r'network|browse|listen|wi.?fi|cellular|interface|reachab|tunnel|advertis|bonjour|path|suspend|stop|start|policy|pair', re.I)

def redact(message):
    message = re.sub(r'\b(?:\d{1,3}\.){3}\d{1,3}\b', '<IP>', message)
    message = re.sub(r'\b[0-9A-Fa-f]{8}-[0-9A-Fa-f-]{12,}\b', '<ID>', message)
    message = re.sub(r'\b[A-Za-z0-9+/=_-]{32,}\b', '<OPAQUE>', message)
    return message[:2000]

async def main():
    config = json.loads(Path('/etc/simlocation/network.json').read_text())
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open('http://127.0.0.1:49151/', timeout=5) as response:
        entries = json.load(response).get(config['udid'], [])
    if not entries: raise SystemExit('NO_ACTIVE_TUNNEL')
    entry = entries[0]
    async with RemoteServiceDiscoveryService((entry['tunnel-address'], entry['tunnel-port'])) as rsd:
        async with OsTraceService(rsd) as trace:
            processes = (await asyncio.wait_for(trace.get_pid_list(), 10)).get('Payload', {})
        candidates = [(int(pid), meta['ProcessName']) for pid, meta in processes.items()
                      if isinstance(meta, dict) and meta.get('ProcessName') in NAMES]
        if not candidates: raise SystemExit('PAIRING_PROCESS_NOT_FOUND')
        pid, name = sorted(candidates, key=lambda item: item[1] != 'remotepairingdeviced')[0]
        print('TARGET_PROCESS', name, flush=True)
        os.umask(0o077)
        fd = os.open(LOG_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        started = time.monotonic()
        with os.fdopen(fd, 'w') as out:
            count = 0
            def record(kind, **fields):
                nonlocal count
                if count >= 1000: return
                out.write(json.dumps({'elapsed': round(time.monotonic()-started, 1), 'kind': kind, **fields})+'\n')
                out.flush(); count += 1
            async def logs():
                async with OsTraceService(rsd) as trace:
                    async for item in trace.syslog(pid=pid):
                        if item.pid == pid and INTEREST.search(item.message):
                            record('device', message=redact(item.message))
            async def probes():
                previous = None
                while True:
                    try:
                        _, writer = await asyncio.wait_for(asyncio.open_connection(config['host'], config['port']), 2)
                        writer.close(); await writer.wait_closed(); state = 'open'
                    except Exception as exc: state = type(exc).__name__
                    record('control_port', state=state)
                    if state != previous:
                        print('CONTROL_PORT', state, flush=True); previous = state
                    await asyncio.sleep(5)
            tasks = [asyncio.create_task(logs()), asyncio.create_task(probes())]
            try:
                await asyncio.sleep(2)
                if tasks[0].done(): await tasks[0]
                print('DIAGNOSTIC_STREAM_ACTIVE; bounded to 180 seconds', flush=True)
                done, _ = await asyncio.wait(tasks, timeout=178, return_when=asyncio.FIRST_EXCEPTION)
                for task in done: await task
            finally:
                for task in tasks: task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
            print('DIAGNOSTIC_FINISHED', count, 'sanitized records', flush=True)

asyncio.run(main())
