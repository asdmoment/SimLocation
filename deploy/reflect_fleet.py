"""Four-node relay packaging and fleet operations; no location writes."""
import base64
from datetime import datetime, timedelta, timezone
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import socket
import zipfile

HOST_NAME = re.compile(r'[A-Za-z0-9_-]{1,64}')
SOURCE_FILES = ('reflect_relay.py', 'reflect_tunneld.py', 'reflect_session.py', 'REFLECT.md', 'FLEET-RELAY.md')
SERVER_FILES = ('ca.pem', 'server.pem', 'server-key.pem', 'expiry.json')


def private_write(path, data):
    if isinstance(data, str):
        data = data.encode()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(data)


def host_names(inventory):
    names = list(inventory['hosts'])
    if not names or len(names) > 16 or any(not HOST_NAME.fullmatch(n) for n in names):
        raise ValueError('Fleet needs 1..16 hosts with safe unique names')
    addresses = [ipaddress.IPv4Address(h['address']) for h in inventory['hosts'].values()]
    if len(set(addresses)) != len(addresses):
        raise ValueError('Duplicate private server address')
    return names


def public_ipv4(endpoint):
    try:
        ip = ipaddress.IPv4Address(endpoint)
    except ipaddress.AddressValueError:
        entries = socket.getaddrinfo(endpoint, 8446, socket.AF_INET, socket.SOCK_STREAM)
        addresses = {row[4][0] for row in entries}
        if len(addresses) != 1:
            raise ValueError('Relay endpoint must resolve to one explicit IPv4 address')
        ip = ipaddress.IPv4Address(addresses.pop())
    if not ip.is_global:
        raise ValueError('Relay endpoint must be a public IPv4 address')
    return str(ip)


def make_credentials(name, now, days):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    expires = now + timedelta(days=days)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'SimLocation relay ' + name)])
    ca = (x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name)
          .public_key(ca_key.public_key()).serial_number(x509.random_serial_number())
          .not_valid_before(now - timedelta(minutes=5)).not_valid_after(expires)
          .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
          .add_extension(x509.KeyUsage(False, False, False, False, False, True, True, False, False), critical=True)
          .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
          .sign(ca_key, hashes.SHA256()))
    # Python >= 3.13 sets VERIFY_X509_STRICT, which rejects certificates without these identifiers.
    authority = x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key())
    result = {'ca.pem': ca.public_bytes(serialization.Encoding.PEM),
              'expiry.json': (json.dumps({'not_after_utc': expires.isoformat()}) + '\n').encode()}
    for role, usage in [('server', ExtendedKeyUsageOID.SERVER_AUTH), ('phone', ExtendedKeyUsageOID.CLIENT_AUTH)]:
        key = ec.generate_private_key(ec.SECP256R1())
        cert = (x509.CertificateBuilder()
                .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name + '-' + role)]))
                .issuer_name(ca_name).public_key(key.public_key()).serial_number(x509.random_serial_number())
                .not_valid_before(now - timedelta(minutes=5)).not_valid_after(expires)
                .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
                .add_extension(x509.ExtendedKeyUsage([usage]), critical=True)
                .add_extension(x509.KeyUsage(True, False, False, False, False, False, False, False, False), critical=True)
                .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
                .add_extension(authority, critical=False))
        if role == 'server':
            cert = cert.add_extension(x509.SubjectAlternativeName([x509.DNSName('reflect-relay.invalid')]), critical=False)
        result[role + '.pem'] = cert.sign(ca_key, hashes.SHA256()).public_bytes(serialization.Encoding.PEM)
        result[role + '-key.pem'] = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                     serialization.NoEncryption())
    # Each host has an independent CA. CA private keys are not persisted or deployed.
    return result


def prepare(inventory, output, endpoints, days=30):
    names = host_names(inventory)
    if type(days) is not int or not 1 <= days <= 90:
        raise ValueError('Certificate days must be between 1 and 90')
    if set(endpoints) != set(names):
        raise ValueError('Endpoints must match the inventory')
    addresses = {n: public_ipv4(endpoints[n]) for n in names}
    out = Path(output).expanduser().absolute()
    if any(p.is_symlink() for p in (out, *out.parents)):
        raise ValueError('Output path must not contain symlinks')
    out.mkdir(mode=0o700)  # Never overwrite or reuse an existing credential bundle.
    phone = out / 'Reflect-Relay-Fleet'
    phone.mkdir(mode=0o700)
    servers = out / 'servers'
    servers.mkdir(mode=0o700)
    now = datetime.now(timezone.utc)
    nodes = []
    for name in names:
        credentials = make_credentials(name, now, days)
        server_dir, phone_dir = servers / name, phone / name
        server_dir.mkdir(mode=0o700)
        phone_dir.mkdir(mode=0o700)
        for filename in SERVER_FILES:
            private_write(server_dir / filename, credentials[filename])
        for filename in ('ca.pem', 'phone.pem', 'phone-key.pem'):
            private_write(phone_dir / filename, credentials[filename])
        nodes.append({'name': name, 'server': addresses[name], 'port': 8446,
                      'ca': name + '/ca.pem', 'certificate': name + '/phone.pem',
                      'private_key': name + '/phone-key.pem'})
    # No phone-side limit: the servers decide how long a session lasts (relay-start).
    private_write(phone / 'phone.json', json.dumps({'servers': nodes, 'reconnect': True,
                                                   'max_seconds': None}, indent=2) + '\n')
    private_write(phone / 'reflect_relay.py', Path(__file__).with_name('reflect_relay.py').read_bytes())
    private_write(phone / 'README.txt',
                  'Private phone package; contains per-host TLS client keys.\n'
                  'Keep Surge and its reflect module enabled, connect Wi-Fi, use pickFolder in a-Shell.\n'
                  'Run: python3 reflect_relay.py phone --config phone.json\n'
                  'Each [host] RELAY_READY is an independent outer connection, not device readiness.\n'
                  'Keep a-Shell foreground. Only one VPS may hold a location session at a time.\n'
                  'To exclude a failed or former primary after fencing: add --nodes NAME [NAME ...].\n'
                  'Stopping a relay does not explicitly clear location. Cellular fresh device\n'
                  'connections and background/lock-screen survival are not established.\n'
                  'Do not install these CA files into iOS or Surge MITM trust.\n')
    manifest = {'schema': 1, 'created_at': now.isoformat(), 'expires_at': (now + timedelta(days=days)).isoformat(),
                'hosts': {n: {'public_ip': addresses[n], 'web_url':
                             f"http://{inventory['hosts'][n]['address']}:{inventory['defaults']['web_port']}"}
                          for n in names}}
    private_write(out / 'manifest.json', json.dumps(manifest, indent=2) + '\n')
    archive = out / 'Reflect-Relay-Fleet.zip'
    with archive.open('xb') as stream:
        os.chmod(archive, 0o600)
        with zipfile.ZipFile(stream, 'w', compression=zipfile.ZIP_DEFLATED) as zf:
            for path in sorted(phone.rglob('*')):
                if path.is_file():
                    zf.write(path, path.relative_to(out))
    return archive


def bundle_params(inventory, name, root):
    """Only this host's server identity crosses SSH; no client or CA private key."""
    root = Path(root).expanduser().absolute()
    manifest = json.loads((root / 'manifest.json').read_text())
    if manifest.get('schema') != 1 or set(manifest.get('hosts', {})) != set(host_names(inventory)):
        raise ValueError('Credential bundle does not match this fleet')
    if datetime.fromisoformat(manifest['expires_at']) <= datetime.now(timezone.utc) + timedelta(hours=3):
        raise ValueError('Prepare renewed credentials before deploying this bundle')
    files = {n: Path(__file__).with_name(n).read_text() for n in SOURCE_FILES}
    credentials = {}
    for filename in SERVER_FILES:
        path = root / 'servers' / name / filename
        if any(p.is_symlink() for p in (path, *path.parents)) or not path.is_file():
            raise ValueError('Credential source must be a regular non-symlink path')
        if path.stat().st_mode & 0o077:
            raise ValueError('Credential source files must be private')
        credentials[filename] = base64.b64encode(path.read_bytes()).decode('ascii')
    return {'action': 'install', 'files': files, 'credentials': credentials,
            'public_ip': public_ipv4(manifest['hosts'][name]['public_ip'])}
