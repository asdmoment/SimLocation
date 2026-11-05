#!/usr/bin/env python3
"""Build a private phone package from an existing, unexpired mTLS phone bundle."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import zipfile

README = """Reflect Relay Phone

This package contains a private client key. Keep it on your own devices.
Do not install ca.pem into iOS trust settings or Surge MITM.

1. Connect the phone to Wi-Fi; keep Surge and the reflect module enabled.
2. In a-Shell run pickFolder and select this extracted folder.
3. Run: python3 reflect_relay.py phone --config phone.json
4. Keep a-Shell in the foreground. RELAY_READY means only the outer server
   connection is ready. The web console must also show the device tunnel online.
5. From a second device, use your existing private SimLocation web console to
   set a location, run a route, or restore the real location. These are explicit
   manual actions. Opening Safari on this same phone backgrounds a-Shell;
   reliable background operation has not been established.
6. Clear the simulated location in the console before ending a normal session,
   then press Ctrl+C in a-Shell. Ending this script is not a location-clear action.

Transport disconnects retry every five seconds within the session time limit.
Certificate verification failures stop; use a renewed package after expiry.
A phone_port_refused / PHONE_SOCKET_FAILED error on cellular means the inner
phone connection failed. Reconnect to Wi-Fi to establish a fresh device tunnel.
No seamless cellular handoff or iOS background/lock-screen survival is promised.
The server session also has its own expiry; its rollback restores the previous
backend and removes the temporary public relay port.
"""


def build(source, output, seconds=3600):
    source, output = Path(source).resolve(), Path(output).absolute()
    if not 300 <= seconds <= 7200:
        raise ValueError('seconds must be between 300 and 7200')
    archive = output.with_suffix('.zip')
    if output.exists() or archive.exists():
        raise FileExistsError('Choose a new output directory and archive name')
    config = json.loads((source / 'phone.json').read_text())
    expected = {'ca': 'ca.pem', 'certificate': 'phone.pem', 'private_key': 'phone-key.pem'}
    for key, name in expected.items():
        if config.get(key) != name:
            raise ValueError('Unexpected certificate path in source phone.json')
        path = source / name
        if path.is_symlink() or not path.is_file():
            raise ValueError('Certificate files must be regular local files')
    for name in ('ca.pem', 'phone.pem'):
        subprocess.run(['openssl', 'x509', '-in', str(source / name), '-noout',
                        '-checkend', str(seconds + 300)], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    os.umask(0o077)
    output.mkdir(mode=0o700)
    for name in expected.values():
        shutil.copyfile(source / name, output / name)
        (output / name).chmod(0o600)
    shutil.copyfile(Path(__file__).with_name('reflect_relay.py'), output / 'reflect_relay.py')
    config.update(reconnect=True, max_seconds=seconds)
    (output / 'phone.json').write_text(json.dumps(config, indent=2) + '\n')
    (output / 'README.txt').write_text(README)
    with zipfile.ZipFile(archive, 'x', compression=zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(output.iterdir()):
            zf.write(path, str(Path(output.name) / path.name))
    archive.chmod(0o600)
    return archive


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--seconds', type=int, default=3600)
    args = parser.parse_args()
    print(build(args.source, args.output, args.seconds))


if __name__ == '__main__':
    main()
