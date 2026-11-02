"""Logic of the fleet tool; nothing here touches SSH, systemd or a real host."""

import base64
import csv
import importlib.util
import io
import json
import stat
import tarfile
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

HERE = Path(__file__).resolve().parent


def load(name):
    spec = importlib.util.spec_from_file_location(name, HERE / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


remote = load("fleet_remote")
fleet = load("fleet")
SERVER_KEY = "HIgo9xNzJMWLKASShiTqIybxZ0U3wGLiUeJ1PKf8ykw="
PHONE_KEY = "xTIBA5rboUvnH4htodjb6e697QjLERt1NAB4mZqp8Dg="


class WireGuardConfigTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.conf = Path(self.directory.name) / "wg-simloc.conf"
        self.patch = patch.object(remote, "WG_CONF", self.conf)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.directory.cleanup()

    def test_new_host_gets_a_key_and_no_peer_until_the_phone_key_exists(self):
        with patch.object(remote, "run", return_value=SimpleNamespace(stdout=SERVER_KEY + "\n")) as run:
            text, changed, private = remote.wireguard_config("10.77.77.11", 51820, "10.77.77.2", None, 1280)
        run.assert_called_once_with("wg", "genkey")
        self.assertEqual(private, SERVER_KEY)
        self.assertIn("Address = 10.77.77.11/32\n", text)
        self.assertNotIn("[Peer]", text)
        self.assertTrue(changed)

    def test_existing_prefix_and_key_are_kept_so_the_interface_is_not_restarted(self):
        self.conf.write_text(f"[Interface]\nAddress = 10.77.77.1/30\nListenPort = 51820\nPrivateKey = {SERVER_KEY}\n"
                             f"MTU = 1280\n\n[Peer]\nPublicKey = {PHONE_KEY}\nAllowedIPs = 10.77.77.2/32\n")
        with patch.object(remote, "run") as run:
            text, changed, _ = remote.wireguard_config("10.77.77.1", 51820, "10.77.77.2", None, 1280)
            run.assert_not_called()
        self.assertFalse(changed)
        self.assertIn("Address = 10.77.77.1/30", text)
        self.assertIn(f"PublicKey = {PHONE_KEY}\nAllowedIPs = 10.77.77.2/32", text)
        rotated = PHONE_KEY[:-2] + "g="
        text, changed, _ = remote.wireguard_config("10.77.77.1", 51820, "10.77.77.2", rotated, 1280)
        self.assertFalse(changed, "a new phone key only touches the peer")
        self.assertIn(f"PublicKey = {rotated}", text)
        with self.assertRaises(RuntimeError):
            remote.wireguard_config("10.77.77.1", 51820, "10.77.77.2", "not a key", 1280)


class RemoteHelperTests(unittest.TestCase):
    def test_nft_rules_name_the_service_accounts_and_addresses(self):
        with patch.object(remote, "run"), patch.object(remote, "write_file", return_value=True) as write:
            self.assertTrue(remote.configure_nft(990, 991, "10.77.77.2", "10.77.77.11", 8765))
        text = write.call_args.args[1]
        self.assertIn("elements = { 0, 990, 991 }", text)
        self.assertIn("ip daddr 10.77.77.2 meta skuid != @allowed", text)
        self.assertIn("ip daddr 10.77.77.11 tcp dport 8765 meta skuid != 0", text)
        self.assertTrue(text.splitlines()[2:4] == ["table inet simlocation", "delete table inet simlocation"],
                        "reload replaces only our table")

    def test_install_code_swaps_trees_only_on_change_and_keeps_one_previous(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            bundle = self.bundle({"bin/simlocation.py": b"one", "web/console.js": b"js"})
            with patch.object(remote, "APP", base / "app"), patch.object(remote, "PREVIOUS", base / "app.prev"), \
                 patch.object(remote.os, "chown"):
                self.assertEqual(remote.install_code(bundle, "abc"), ["REVISION", "bin/simlocation.py", "web/console.js"])
                self.assertEqual(remote.install_code(bundle, "abc"), [])
                changed = remote.install_code(self.bundle({"bin/simlocation.py": b"two", "web/console.js": b"js"}), "def")
                self.assertEqual(changed, ["REVISION", "bin/simlocation.py"])
                self.assertEqual((base / "app.prev" / "bin/simlocation.py").read_bytes(), b"one")
                self.assertEqual(sorted(p.name for p in base.iterdir()), ["app", "app.prev"], "no staging left")

    @staticmethod
    def bundle(files):
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
            for name, data in files.items():
                info = tarfile.TarInfo(name)
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
        return base64.b64encode(buffer.getvalue()).decode()

    def test_password_hash_format_is_checked_and_errors_come_back_as_json(self):
        with self.assertRaises(RuntimeError):
            remote.set_password({"password_hash": "plain\nSIMLOCATION_MAP_LISTEN=0.0.0.0"})
        output = io.StringIO()
        with patch("sys.stdout", output):
            remote.main("set_password", {"password_hash": "bad"})
        result = json.loads(output.getvalue().split(remote.RESULT_MARKER, 1)[1])
        self.assertFalse(result["ok"])
        self.assertIn("unexpected password hash format", result["error"])


class FleetTests(unittest.TestCase):
    def inventory(self):
        return {"phone": {"udid": "u", "alias": "a", "address": "10.77.77.2", "public_key": None},
                "defaults": {**fleet.DEFAULTS},
                "hosts": {"one": {"ssh": ["one"], "endpoint": "198.51.100.1", "address": "10.77.77.1", "label": "One"},
                          "two": {"ssh": ["-J", "one", "two"], "endpoint": "auto", "address": "10.77.77.11"}}}

    def test_bundle_carries_code_web_and_adapter_only(self):
        bundle, revision = fleet.build_bundle()
        with tarfile.open(fileobj=io.BytesIO(base64.b64decode(bundle))) as archive:
            names = set(archive.getnames())
        self.assertTrue({"bin/simlocation.py", "web/console.html", "web/login.html", "deploy/network_tunneld.py"} <= names)
        self.assertFalse(any(name.startswith(("var/", "tests/", "deploy/fleet")) for name in names))
        self.assertTrue(revision)

    def test_auto_endpoint_comes_from_ssh_config(self):
        resolved = SimpleNamespace(stdout="user root\nhostname 203.0.113.9\nport 22\n")
        with patch.object(fleet.subprocess, "run", return_value=resolved) as run:
            self.assertEqual(fleet.endpoint({"ssh": ["-J", "one", "two"], "endpoint": "auto"}), "203.0.113.9")
        self.assertEqual(run.call_args.args[0], ["ssh", "-G", "two"])
        self.assertEqual(fleet.endpoint({"ssh": ["x"], "endpoint": "192.0.2.1"}), "192.0.2.1")

    def test_phone_configs_are_private_and_route_only_the_servers(self):
        inventory = self.inventory()
        answers = {"one": {"server_public_key": SERVER_KEY, "listen_port": 51820, "address": "10.77.77.1"},
                   "two": {"server_public_key": PHONE_KEY, "listen_port": 51820, "address": "10.77.77.11"}}
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(fleet, "remote", side_effect=lambda inv, name, *a, **k: answers[name]), \
             patch.object(fleet, "save_inventory") as save, \
             patch.object(fleet.subprocess, "run", return_value=SimpleNamespace(stdout="hostname 203.0.113.9\n")):
            fleet.cmd_phone(inventory, SimpleNamespace(out=directory, no_open=True))
            out = Path(directory)
            combined = (out / "SimLocation-all.conf").read_text()
            single = (out / "SimLocation-two.conf").read_text()
            modes = {path.name: stat.S_IMODE(path.stat().st_mode) for path in out.iterdir()}
        save.assert_called_once()
        self.assertTrue(inventory["phone"]["public_key"])
        self.assertNotIn("PrivateKey", json.dumps(inventory))
        self.assertEqual(combined.count("[Peer]"), 2)
        self.assertIn("AllowedIPs = 10.77.77.1/32", combined)
        self.assertIn("Endpoint = 203.0.113.9:51820", single)
        self.assertNotIn("0.0.0.0/0", combined)
        self.assertNotIn("DNS", combined)
        self.assertEqual(set(modes.values()), {0o600})
        self.assertIn("SimLocation-all.png", modes)

    def test_password_csv_matches_the_hash_sent_to_each_host(self):
        inventory = self.inventory()
        sent = {}
        web = fleet.load_web_module()
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(fleet, "remote", side_effect=lambda inv, name, fn, params, **k: sent.setdefault(name, params)):
            fleet.cmd_password(inventory, SimpleNamespace(hosts=[], out=directory))
            with (Path(directory) / "simlocation-passwords.csv").open(newline="") as handle:
                rows = list(csv.reader(handle))
        self.assertEqual(rows[0], ["Title", "URL", "Username", "Password", "Notes", "OTPAuth"])
        self.assertEqual(rows[1][1], "http://10.77.77.1:8765/")
        for row, name in zip(rows[1:], ("one", "two")):
            self.assertEqual(len(row[3]), 27)
            self.assertTrue(web.verify_password(row[3], web.parse_password_hash(sent[name]["password_hash"])))
        self.assertNotEqual(rows[1][3], rows[2][3])


if __name__ == "__main__":
    unittest.main()
