#!/usr/bin/env python3
"""Read-only RSD/DVT smoke test. Never sets or clears location."""
import asyncio
import json
import urllib.request
from pathlib import Path

from pymobiledevice3.remote.remote_service_discovery import RemoteServiceDiscoveryService
from pymobiledevice3.services.dvt.instruments.dvt_provider import DvtProvider
from pymobiledevice3.services.dvt.instruments.location_simulation import LocationSimulation


async def main():
    config = json.loads(Path('/var/lib/simlocation/devices.json').read_text())
    udid = config.get('aliases', {}).get(config.get('default'), config.get('default'))
    if not udid:
        raise SystemExit('NO_DEFAULT_DEVICE')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open('http://127.0.0.1:49151/', timeout=5) as response:
        entries = json.load(response).get(udid, [])
    if not entries:
        raise SystemExit('DEVICE_OFFLINE: no registered tunnel')
    entry = entries[0]
    async with asyncio.timeout(30):
        async with RemoteServiceDiscoveryService(
            (entry['tunnel-address'], entry['tunnel-port'])
        ) as rsd:
            print('RSD_CONNECTED', flush=True)
            async with DvtProvider(rsd) as dvt:
                print('DVT_CONNECTED', flush=True)
                async with LocationSimulation(dvt):
                    print('LOCATION_CHANNEL_OPEN; no coordinates sent', flush=True)
    print('READ_ONLY_VERIFICATION_COMPLETE', flush=True)


if __name__ == '__main__':
    asyncio.run(main())
