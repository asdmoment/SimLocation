#!/usr/bin/env python3
"""Four-node relay packaging, credential routing and phone fleet config; no SSH or device."""
import asyncio
from datetime import datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path
import ssl
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import zipfile


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


fleet = load('reflect_fleet', 'reflect_fleet.py')
relay = load('relay_for_fleet', 'reflect_relay.py')

INVENTORY = {
    'defaults': {'web_port': 8765},
    'hosts': {
        'alpha': {'address': '10.77.77.1'},
        'beta': {'address': '10.77.77.11'},
    },
}
ENDPOINTS = {'alpha': '198.51.100.1', 'beta': '198.51.100.2'}
GLOBAL = {'198.51.100.1': '8.8.8.8', '198.51.100.2': '1.1.1.1'}  # documentation range is not global


def prepare(root, days=30):
    endpoints = {name: GLOBAL[ip] for name, ip in ENDPOINTS.items()}
    return fleet.prepare(INVENTORY, root / 'bundle', endpoints, days)


class PrepareTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()

    def tearDown(self):
        self.tmp.cleanup()

    def test_package_layout_and_privacy(self):
        archive = prepare(self.root)
        out = archive.parent
        for path in out.rglob('*'):
            mask = 0o700 if path.is_dir() else 0o600
            self.assertEqual(path.stat().st_mode & 0o777, mask, path)
        with zipfile.ZipFile(archive) as zf:
            names = set(zf.namelist())
        self.assertIn('Reflect-Relay-Fleet/phone.json', names)
        self.assertIn('Reflect-Relay-Fleet/reflect_relay.py', names)
        for host in INVENTORY['hosts']:
            self.assertIn(f'Reflect-Relay-Fleet/{host}/phone-key.pem', names)
        # Server identities and the manifest never travel to the phone.
        self.assertFalse(any('server' in n.rsplit('/', 1)[-1] for n in names))
        self.assertNotIn('manifest.json', names)
        phone = json.loads((out / 'Reflect-Relay-Fleet/phone.json').read_text())
        self.assertEqual([n['name'] for n in phone['servers']], ['alpha', 'beta'])
        self.assertEqual(phone['servers'][0]['server'], '8.8.8.8')
        self.assertTrue(phone['reconnect'])
        manifest = json.loads((out / 'manifest.json').read_text())
        self.assertEqual(manifest['hosts']['beta']['web_url'], 'http://10.77.77.11:8765')

    def test_each_host_has_an_independent_ca(self):
        out = prepare(self.root).parent
        phone = out / 'Reflect-Relay-Fleet'
        for host in ('alpha', 'beta'):
            ok = subprocess.run(['openssl', 'verify', '-CAfile', phone / host / 'ca.pem', phone / host / 'phone.pem'],
                                capture_output=True)
            self.assertEqual(ok.returncode, 0, ok.stderr)
        crossed = subprocess.run(['openssl', 'verify', '-CAfile', phone / 'alpha/ca.pem', phone / 'beta/phone.pem'],
                                 capture_output=True)
        self.assertNotEqual(crossed.returncode, 0)
        server = out / 'servers/alpha'
        ok = subprocess.run(['openssl', 'verify', '-purpose', 'sslserver', '-verify_hostname', 'reflect-relay.invalid',
                             '-CAfile', server / 'ca.pem', server / 'server.pem'], capture_output=True)
        self.assertEqual(ok.returncode, 0, ok.stderr)

    def test_mutual_tls_under_strict_x509(self):
        """Python >= 3.13 (vps-b runs 3.14) rejects certificates without key identifiers."""
        import socket
        import threading
        out = prepare(self.root).parent
        server, phone = out / 'servers/alpha', out / 'Reflect-Relay-Fleet/alpha'
        server_ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH, cafile=server / 'ca.pem')
        server_ctx.verify_mode = ssl.CERT_REQUIRED
        server_ctx.load_cert_chain(server / 'server.pem', server / 'server-key.pem')
        client_ctx = ssl.create_default_context(cafile=phone / 'ca.pem')
        client_ctx.load_cert_chain(phone / 'phone.pem', phone / 'phone-key.pem')
        for ctx in (server_ctx, client_ctx):
            ctx.minimum_version = ssl.TLSVersion.TLSv1_3
            ctx.verify_flags |= getattr(ssl, 'VERIFY_X509_STRICT', 0)
        listener = socket.create_server(('127.0.0.1', 0))
        result = {}

        def serve():
            conn, _ = listener.accept()
            with server_ctx.wrap_socket(conn, server_side=True) as tls:
                result['peer'] = tls.getpeercert()['subject']
                tls.sendall(b'ok')

        thread = threading.Thread(target=serve)
        thread.start()
        with socket.create_connection(listener.getsockname()) as raw:
            with client_ctx.wrap_socket(raw, server_hostname='reflect-relay.invalid') as tls:
                self.assertEqual(tls.recv(2), b'ok')
        thread.join(5)
        listener.close()
        self.assertIn(((('commonName', 'alpha-phone'),),), [result['peer']])

    def test_refuses_reuse_bad_days_and_mismatched_endpoints(self):
        prepare(self.root)
        with self.assertRaises(FileExistsError):
            prepare(self.root)
        for days in (0, 91, '30'):
            with self.assertRaises(ValueError):
                fleet.prepare(INVENTORY, self.root / f'd{days}', GLOBAL, days)
        with self.assertRaises(ValueError):
            fleet.prepare(INVENTORY, self.root / 'x', {'alpha': '8.8.8.8'}, 30)
        with self.assertRaisesRegex(ValueError, 'public'):
            fleet.prepare(INVENTORY, self.root / 'y', ENDPOINTS, 30)

    def test_bundle_params_send_only_that_host_server_identity(self):
        out = prepare(self.root).parent
        params = fleet.bundle_params(INVENTORY, 'beta', out)
        self.assertEqual(set(params['credentials']), set(fleet.SERVER_FILES))
        self.assertEqual(set(params['files']), set(fleet.SOURCE_FILES))
        self.assertEqual(params['public_ip'], '1.1.1.1')
        import base64
        sent = base64.b64decode(params['credentials']['server.pem'])
        self.assertEqual(sent, (out / 'servers/beta/server.pem').read_bytes())
        self.assertNotIn(b'phone', b''.join(base64.b64decode(v) for v in params['credentials'].values()))

    def test_bundle_params_refuse_expiring_loose_or_foreign_bundle(self):
        out = prepare(self.root).parent
        (out / 'servers/alpha/server-key.pem').chmod(0o644)
        with self.assertRaisesRegex(ValueError, 'private'):
            fleet.bundle_params(INVENTORY, 'alpha', out)
        other = {'defaults': INVENTORY['defaults'], 'hosts': {'alpha': INVENTORY['hosts']['alpha']}}
        with self.assertRaisesRegex(ValueError, 'match'):
            fleet.bundle_params(other, 'alpha', out)
        manifest = json.loads((out / 'manifest.json').read_text())
        manifest['expires_at'] = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        (out / 'manifest.json').write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, 'renewed'):
            fleet.bundle_params(INVENTORY, 'beta', out)


class PhoneFleetConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, servers, **extra):
        path = self.root / 'phone.json'
        path.write_text(json.dumps({'servers': servers, **extra}))
        return path

    @staticmethod
    def node(name, prefix=None):
        prefix = prefix or name
        return {'name': name, 'server': '8.8.8.8', 'port': 8446, 'ca': prefix + '/ca.pem',
                'certificate': prefix + '/phone.pem', 'private_key': prefix + '/phone-key.pem'}

    def test_generated_package_loads(self):
        out = prepare(self.root).parent
        config = relay.load_config(out / 'Reflect-Relay-Fleet/phone.json')
        self.assertEqual(len(config['servers']), 2)
        self.assertIsNone(config['max_seconds'])  # servers decide the session length
        for node in config['servers']:
            self.assertTrue(Path(node['ca']).is_file())

    def test_rejects_bad_limits(self):
        for limit in (0, 59, 86401, '600', True):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                relay.load_config(self.write([self.node('a')], max_seconds=limit))

    def test_rejects_escape_duplicates_and_bad_ports(self):
        cases = [[self.node('a', '../a')], [self.node('a'), self.node('a')],
                 [{**self.node('a'), 'port': 0}], [{**self.node('a'), 'name': 'a b'}], [],
                 [self.node(f'n{i}') for i in range(17)], [{**self.node('a'), 'server': ' '}]]
        for servers in cases:
            with self.subTest(servers=servers), self.assertRaises(ValueError):
                relay.load_config(self.write(servers))


class FleetRunnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_one_failed_node_does_not_stop_others(self):
        seen = []

        async def fake(node):
            seen.append((node['name'], node['reconnect'], node['max_seconds']))
            if node['name'] == 'bad':
                raise ssl.SSLCertVerificationError('expired')

        with patch.object(relay, 'run_phone', fake), patch.object(relay, 'report'):
            await relay.run_phone_fleet({'servers': [{'name': 'bad'}, {'name': 'good'}], 'max_seconds': 600})
        self.assertEqual(sorted(seen), [('bad', True, 600), ('good', True, 600)])

    async def test_all_nodes_failed_is_an_error(self):
        async def fake(node):
            raise ConnectionRefusedError()

        with patch.object(relay, 'run_phone', fake), patch.object(relay, 'report'):
            with self.assertRaises(relay.RelayError):
                await relay.run_phone_fleet({'servers': [{'name': 'a'}, {'name': 'b'}]})

    async def test_reports_are_prefixed_per_node(self):
        lines = []
        with patch('builtins.print', lambda *a, **k: lines.append(' '.join(map(str, a)))):
            async def fake(node):
                relay.report('RELAY_READY')

            with patch.object(relay, 'run_phone', fake):
                await relay.run_phone_fleet({'servers': [{'name': 'a'}, {'name': 'b'}]})
        self.assertEqual(sorted(lines), ['[a] RELAY_READY', '[b] RELAY_READY'])


if __name__ == '__main__':
    unittest.main()
