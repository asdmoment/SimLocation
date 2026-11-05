import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

spec = importlib.util.spec_from_file_location('phone_builder', Path(__file__).with_name('prepare_reflect_phone.py'))
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


class PackageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / 'source'
        self.source.mkdir()
        self.config = {'server': '192.0.2.1', 'port': 8446, 'ca': 'ca.pem',
                       'certificate': 'phone.pem', 'private_key': 'phone-key.pem'}
        (self.source / 'phone.json').write_text(json.dumps(self.config))
        for name in ('ca.pem', 'phone.pem', 'phone-key.pem'):
            (self.source / name).write_text('fixture only')
        self.output = self.root / 'phone-v2'

    @patch.object(builder.subprocess, 'run')
    def test_copies_private_bundle_without_pairing_data(self, run):
        archive = builder.build(self.source, self.output)
        config = json.loads((self.output / 'phone.json').read_text())
        self.assertTrue(config['reconnect'])
        self.assertEqual(config['max_seconds'], 3600)
        self.assertEqual(config['server'], '192.0.2.1')
        self.assertEqual((self.output / 'phone-key.pem').stat().st_mode & 0o777, 0o600)
        self.assertEqual(archive.stat().st_mode & 0o777, 0o600)
        with zipfile.ZipFile(archive) as zf:
            self.assertEqual(len(zf.namelist()), 6)
            self.assertTrue(all(n.startswith('phone-v2/') for n in zf.namelist()))
        self.assertEqual(run.call_count, 2)

    def test_does_not_overwrite_package(self):
        self.output.mkdir()
        with self.assertRaises(FileExistsError): builder.build(self.source, self.output)

    def test_rejects_arbitrary_certificate_paths(self):
        self.config['private_key'] = '../other.pem'
        (self.source / 'phone.json').write_text(json.dumps(self.config))
        with self.assertRaises(ValueError): builder.build(self.source, self.output)
        self.assertFalse(self.output.exists())

    def test_rejects_certificate_symlinks(self):
        (self.source / 'phone-key.pem').unlink()
        (self.source / 'phone-key.pem').symlink_to(self.source / 'phone.pem')
        with self.assertRaises(ValueError): builder.build(self.source, self.output)

    @patch.object(builder.subprocess, 'run', side_effect=builder.subprocess.CalledProcessError(1, 'openssl'))
    def test_expired_certificate_does_not_create_output(self, run):
        with self.assertRaises(builder.subprocess.CalledProcessError):
            builder.build(self.source, self.output)
        self.assertFalse(self.output.exists())


if __name__ == '__main__':
    unittest.main()
