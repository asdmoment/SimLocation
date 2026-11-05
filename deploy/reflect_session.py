#!/usr/bin/env python3
"""Root-only relay backend session, bounded or persistent; never changes a phone's location."""
import argparse
from datetime import datetime, timezone
import fcntl
import ipaddress
import json
import os
from pathlib import Path
import pwd
import re
import stat
import subprocess
import uuid

BASE = Path('/var/lib/simlocation-relay')
DEPLOY = Path('/opt/simlocation/deploy')
NETWORK = 'simlocation-network.service'
WEB = 'simlocation-web.service'
RELAY = 'simloc-reflect.service'
EXPIRE = 'simlocation-relay-expire'
OLD_STOP = Path('/opt/simloc-reflect-test/stop_trial.py')
PROC = Path('/proc')
DROPINS = {
    NETWORK: Path('/etc/systemd/system/simlocation-network.service.d/60-reflect-session.conf'),
    WEB: Path('/etc/systemd/system/simlocation-web.service.d/zz-reflect-session.conf'),
}
NETWORK_TEXT = '''[Service]
ExecStart=
ExecStart=/opt/simlocation-venv/bin/python -u /opt/simlocation/deploy/reflect_tunneld.py --config /etc/simlocation/network.json
RuntimeDirectory=simloc-reflect-backend
RuntimeDirectoryMode=0700
ReadOnlyPaths=/var/lib/simlocation-tunnel
'''
WEB_TEXT = '[Service]\nEnvironmentFile=/var/lib/simlocation-relay/manual.env\n'
ENV_TEXT = 'SIMLOCATION_WEB_AUTO_SET=0\n'
# A persistent session on the fleet's one auto-set host keeps setting the default point whenever
# the phone's relay comes back (the same behaviour as the WireGuard backend).
AUTO_ENV_TEXT = 'SIMLOCATION_WEB_AUTO_SET=1\n'
WG_RELAY_RULE = re.compile(r'\d+(?:\.\d+){3} 8446/tcp on wg-simloc +ALLOW +\d+(?:\.\d+){3} +# SimLocation relay over WireGuard')
UNIT = Path('/etc/systemd/system/simloc-reflect.service')
RECOVER = Path('/etc/systemd/system/simlocation-relay-recover.service')
RECOVER_TEXT = '''[Unit]
Description=Restore the original SimLocation backend after an interrupted relay session
After=network-online.target simlocation-network.service simlocation-web.service ufw.service
ConditionPathExists=/var/lib/simlocation-relay/session.json
StartLimitIntervalSec=0

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 -I /opt/simlocation/deploy/reflect_session.py disable
Restart=on-failure
RestartSec=15

[Install]
WantedBy=multi-user.target
'''


def run(*args):
    result = subprocess.run(args, text=True, capture_output=True)
    if result.returncode:
        raise RuntimeError('command failed: ' + args[0])
    return result.stdout.strip()


def active(unit):
    return run('systemctl', 'show', unit, '--property=ActiveState', '--value')


def stop(unit):
    if run('systemctl', 'show', unit, '--property=LoadState', '--value') != 'not-found':
        run('systemctl', 'stop', unit)


def safe_path(path):
    """Reject links in every existing component, including dangling links."""
    for part in (path, *path.parents):
        if part.is_symlink():
            raise ValueError('symbolic links are not allowed')


def write_new(path, text):
    safe_path(path)
    with path.open('x', encoding='utf-8') as stream:
        os.chmod(path, 0o600)
        stream.write(text)


def validate(credentials, public_ip, seconds, now=None):
    """seconds=None is a persistent session: it only ends on disable."""
    if seconds is not None and (type(seconds) is not int or not 300 <= seconds <= 7200):
        raise ValueError('seconds must be between 300 and 7200')
    ip = ipaddress.IPv4Address(public_ip)
    if not ip.is_global:
        raise ValueError('public-ip must be a public IPv4 address')
    directory = Path(credentials)
    if not directory.is_absolute():
        raise ValueError('credentials must be an absolute directory')
    safe_path(directory)
    if not directory.is_dir():
        raise ValueError('credentials must be a directory')
    for name in ('ca.pem', 'server.pem', 'server-key.pem', 'expiry.json'):
        path = directory / name
        safe_path(path)
        info = path.stat()
        if not stat.S_ISREG(info.st_mode):
            raise ValueError('credential must be a regular file')
        if name == 'server-key.pem' and (info.st_uid != 0 or stat.S_IMODE(info.st_mode) not in (0o400, 0o600)):
            raise ValueError('private key must be root-owned mode 0400 or 0600')
    expiry = datetime.fromisoformat(json.loads((directory / 'expiry.json').read_text())['not_after_utc'])
    if expiry.tzinfo is None or expiry.utcoffset().total_seconds() != 0:
        raise ValueError('not_after_utc must include UTC timezone')
    now = now or datetime.now(timezone.utc)
    margin = 3 * 3600 if seconds is None else seconds + 300
    if expiry.timestamp() <= now.timestamp() + margin:
        raise ValueError('credentials expire before the session safety margin')
    return directory, str(ip), None if seconds is None else now.timestamp() + seconds


def ensure_no_worker():
    for cmdline in PROC.glob('[0-9]*/cmdline'):
        try:
            if b'--_hold-session' in cmdline.read_bytes().split(b'\0'):
                raise RuntimeError('a location worker is running; stop it explicitly first')
        except FileNotFoundError:
            pass  # A process may exit during the scan.


def preflight():
    ensure_no_worker()
    if active('simloc-reflect-probe.service') != 'inactive':
        raise RuntimeError('reflect probe must be inactive')
    for path in (*DROPINS.values(), BASE / 'manual.env', BASE / 'server.json', BASE / 'firewall.json', RECOVER, UNIT):
        safe_path(path)
        if path.exists():
            raise RuntimeError('session files already exist; run disable first')
    states = {unit: active(unit) for unit in (NETWORK, WEB)}
    if any(value not in ('active', 'inactive', 'failed') for value in states.values()):
        raise RuntimeError('network/web state is changing; retry later')
    # Cancel the old cleanup before invoking it: it must never kill the new relay.
    if OLD_STOP.exists():
        stop('simloc-reflect-expire.timer')
        run('/usr/bin/python3', '-I', str(OLD_STOP))
    if active(RELAY) not in ('inactive', 'failed'):
        raise RuntimeError('an existing relay must be stopped first')
    firewall = run('ufw', 'status')
    if not firewall.startswith('Status: active'):
        raise RuntimeError('UFW must already be active')
    # The fleet's own rule (fleet_remote.configure_ufw) only admits the phone through WireGuard.
    firewall = '\n'.join(line for line in firewall.splitlines() if not WG_RELAY_RULE.fullmatch(line.strip()))
    if re.search(r'(?<!\d)8446(?!\d)', firewall) or any(
            int(low) <= 8446 <= int(high) for low, high in re.findall(r'(\d+):(\d+)', firewall)):
        raise RuntimeError('existing UFW rules for 8446 require manual review')
    return states


RELAY_EXEC = ['/usr/bin/python3', '-I', '-u', str(DEPLOY / 'reflect_relay.py'),
              'server', '--config', '/run/credentials/simloc-reflect.service/config.json']


def relay_properties(credentials, seconds):
    properties = [
        'DynamicUser=yes', 'User=simloc-reflect', 'Group=simlocation-tunnel',
        'RuntimeDirectory=simloc-reflect', 'RuntimeDirectoryMode=0750', 'UMask=0077',
        *([] if seconds is None else [f'RuntimeMaxSec={seconds}']), 'TimeoutStopSec=10', 'NoNewPrivileges=yes',
        'CapabilityBoundingSet=', 'AmbientCapabilities=', 'ProtectSystem=strict',
        'ProtectHome=yes', 'PrivateTmp=yes', 'PrivateDevices=yes',
        'ProtectKernelTunables=yes', 'ProtectKernelModules=yes', 'ProtectKernelLogs=yes',
        'ProtectControlGroups=yes', 'ProtectProc=invisible', 'ProcSubset=pid',
        'RestrictAddressFamilies=AF_INET AF_UNIX', 'RestrictNamespaces=yes',
        'RestrictSUIDSGID=yes', 'LockPersonality=yes', 'RestrictRealtime=yes',
        'SystemCallArchitectures=native', 'SystemCallFilter=@system-service',
        'SystemCallFilter=~@mount @reboot @swap @raw-io @debug', 'MemoryMax=128M',
        'TasksMax=32', 'LimitNOFILE=256', 'CPUQuota=25%',
        'InaccessiblePaths=-/etc/simlocation -/var/lib/simlocation-tunnel -/etc/sing-box -/usr/local/etc/xray -/var/lib/simloc-reflect-test',
        'ReadWritePaths=/run/simloc-reflect',
        f'LoadCredential=config.json:{BASE / "server.json"}',
    ]
    return properties + [f'LoadCredential={name}:{credentials / name}' for name in ('ca.pem', 'server.pem', 'server-key.pem')]


def relay_command(credentials, seconds):
    command = ['systemd-run', '--unit=simloc-reflect', '--collect']
    for prop in relay_properties(credentials, seconds):
        command += ['--property', prop]
    return command + RELAY_EXEC


def relay_unit(credentials):
    """The same sandbox as the transient relay, as a unit that survives reboots and crashes."""
    return '\n'.join([
        '[Unit]', 'Description=SimLocation reflect relay (persistent session)',
        'After=network-online.target', 'Wants=network-online.target', 'StartLimitIntervalSec=0', '',
        '[Service]', *relay_properties(credentials, None), 'Restart=always', 'RestartSec=5',
        'ExecStart=' + ' '.join(RELAY_EXEC), '', '[Install]', 'WantedBy=multi-user.target', ''])


def firewall_command(marker):
    return ['allow', 'proto', 'tcp', 'from', 'any', 'to', marker['ip'],
            'port', '8446', 'comment', marker['comment']]


def disable():
    session = BASE / 'session.json'
    if not session.exists():
        return  # Without our state, do not claim ownership of anything.
    state = json.loads(session.read_text())
    # Close the public entry even if a changed drop-in later needs human review.
    stop(RELAY)
    if state.get('persistent'):
        safe_path(UNIT)
        if UNIT.exists():
            run('systemctl', 'disable', UNIT.name)
            UNIT.unlink()
            run('systemctl', 'daemon-reload')
    marker = BASE / 'firewall.json'
    if marker.exists():
        rule = json.loads(marker.read_text())
        # Only delete our uniquely commented rule; an interrupted add may not exist.
        if rule['comment'] in run('ufw', 'status'):
            run('ufw', '--force', 'delete', *firewall_command(rule))
        marker.unlink()
    # Every operation is retryable. Retain state if any rollback step fails.
    for unit, text in ((NETWORK, NETWORK_TEXT), (WEB, WEB_TEXT)):
        path = DROPINS[unit]
        safe_path(path)
        if path.exists():
            if path.read_text() != text:
                raise RuntimeError('session drop-in changed; refusing to remove it')
            path.unlink()
    run('systemctl', 'daemon-reload')
    # Web Wants=network: restore Web first, then enforce the saved network state.
    for unit in (WEB, NETWORK):
        run('systemctl', 'restart' if state['active'][unit] == 'active' else 'stop', unit)
    for name, text in (('manual.env', state.get('env', ENV_TEXT)), ('server.json', None)):
        path = BASE / name
        safe_path(path)
        if path.exists():
            if text is not None and path.read_text() != text:
                raise RuntimeError('session environment changed; refusing to remove it')
            path.unlink()
    stop(EXPIRE + '.timer')
    if state.get('boot_recovery'):
        safe_path(RECOVER)
        if RECOVER.exists():
            if RECOVER.read_text() != RECOVER_TEXT:
                raise RuntimeError('boot recovery unit changed; refusing to remove it')
            # Do not --now/stop this unit: disable() may be running inside it.
            run('systemctl', 'disable', RECOVER.name)
            RECOVER.unlink()
            run('systemctl', 'daemon-reload')
    session.unlink()


def enable(credentials, public_ip, seconds, auto_set=False):
    if auto_set and seconds is not None:
        raise ValueError('auto-set is only available for a persistent session')
    credentials, ip, deadline = validate(credentials, public_ip, seconds)
    if (BASE / 'session.json').exists():
        raise RuntimeError('a session already exists; run disable first')
    states = preflight()
    persistent = seconds is None
    env = AUTO_ENV_TEXT if auto_set else ENV_TEXT
    state = {'active': states, 'expires_at': deadline, 'boot_recovery': not persistent,
             'persistent': persistent, 'env': env}
    write_new(BASE / 'session.json', json.dumps(state))
    try:
        if not persistent:
            bounded_safety(seconds)
        write_new(BASE / 'manual.env', env)
        config = dict(ca='ca.pem', certificate='server.pem', private_key='server-key.pem',
                      port=8446, socket='/run/simloc-reflect/relay.sock',
                      probe_uid=pwd.getpwnam('simlocation-tunnel').pw_uid, max_seconds=seconds)
        write_new(BASE / 'server.json', json.dumps(config))
        rule = {'ip': ip, 'comment': 'simlocation-relay-' + uuid.uuid4().hex}
        write_new(BASE / 'firewall.json', json.dumps(rule))
        run('ufw', *firewall_command(rule))
        if persistent:
            write_new(UNIT, relay_unit(credentials))
            os.chmod(UNIT, 0o644)
            run('systemctl', 'daemon-reload')
            run('systemctl', 'enable', '--now', UNIT.name)
        else:
            run(*relay_command(credentials, seconds))
        run('systemctl', 'is-active', '--quiet', RELAY)
        switch_backend(states)
    except BaseException:
        disable()
        raise


def bounded_safety(seconds):
    write_new(RECOVER, RECOVER_TEXT)
    run('systemctl', 'daemon-reload')
    # Enable for the next boot only; never start recovery during this session.
    run('systemctl', 'enable', RECOVER.name)
    run('systemd-run', '--unit=' + EXPIRE, '--collect', f'--on-active={seconds}s',
            '--timer-property=AccuracySec=1s', '--property=Restart=on-failure',
            '--property=RestartSec=15s', '--property=StartLimitIntervalSec=0', '/usr/bin/python3', '-I',
        str(DEPLOY / 'reflect_session.py'), 'disable')


def switch_backend(states):
    for unit, text in ((NETWORK, NETWORK_TEXT), (WEB, WEB_TEXT)):
        DROPINS[unit].parent.mkdir(parents=True, exist_ok=True)
        write_new(DROPINS[unit], text)
    run('systemctl', 'daemon-reload')
    env_files = run('systemctl', 'show', WEB, '--property=EnvironmentFiles', '--value')
    if not re.search(re.escape(str(BASE / 'manual.env')) + r' \(ignore_errors=no\)$', env_files):
        raise RuntimeError('manual.env must be the final Web EnvironmentFile')
    stop(WEB)
    ensure_no_worker()  # AutoSetter may have spawned one since preflight.
    run('systemctl', 'restart', NETWORK)
    if states[WEB] == 'active':
        run('systemctl', 'start', WEB)


def status():
    session = BASE / 'session.json'
    if not session.exists():
        return {'session': 'disabled'}
    state = json.loads(session.read_text())
    units = {'relay': active(RELAY), 'network': active(NETWORK), 'web': active(WEB)}
    if state.get('persistent'):
        return {'session': 'persistent', 'remaining_seconds': None,
                'auto_set': state.get('env') == AUTO_ENV_TEXT, **units}
    remaining = max(0, int(state['expires_at'] - datetime.now(timezone.utc).timestamp()))
    return {'session': 'enabled' if remaining else 'expired', 'remaining_seconds': remaining, **units}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    start = commands.add_parser('enable')
    start.add_argument('--credentials', required=True)
    start.add_argument('--public-ip', required=True)
    start.add_argument('--seconds', type=int, default=3600)
    start.add_argument('--persistent', action='store_true', help='no expiry; survives reboots until disable')
    start.add_argument('--auto-set', action='store_true', help='persistent only: keep SIMLOCATION_WEB_AUTO_SET=1')
    commands.add_parser('status')
    commands.add_parser('disable')
    args = parser.parse_args(argv)
    try:
        if os.geteuid() != 0:
            raise PermissionError('must run as root')
        os.umask(0o077)
        safe_path(BASE)
        BASE.mkdir(mode=0o700, exist_ok=True)
        if BASE.stat().st_uid != 0 or stat.S_IMODE(BASE.stat().st_mode) != 0o700:
            raise PermissionError('state directory must be root-owned mode 0700')
        safe_path(BASE / 'lock')
        with (BASE / 'lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if args.command == 'enable':
                enable(args.credentials, args.public_ip, None if args.persistent else args.seconds,
                       auto_set=args.auto_set)
            elif args.command == 'disable':
                disable()
            print(json.dumps(status()))
    except (OSError, ValueError, RuntimeError, KeyError) as exc:
        parser.exit(1, 'reflect session: ' + str(exc) + '\n')


if __name__ == '__main__':
    main()
