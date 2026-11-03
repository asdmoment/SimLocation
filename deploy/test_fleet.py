"""Logic of the fleet tool; nothing here touches SSH, systemd or a real host."""

import base64
import csv
import importlib.util
import io
import json
import plistlib
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


class PeerRouteTests(unittest.TestCase):
    """wg syncconf adds peers without routes; the deploy must add them."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.patch = patch.object(remote, "WG_CONF", Path(self.directory.name) / "wg-simloc.conf")
        self.patch.start()
        self.text = (f"[Interface]\nAddress = 10.77.77.21/32\nListenPort = 51820\nPrivateKey = {SERVER_KEY}\n"
                     f"MTU = 1280\n\n[Peer]\nPublicKey = {PHONE_KEY}\nAllowedIPs = 10.77.77.2/32\n")

    def tearDown(self):
        self.patch.stop()
        self.directory.cleanup()

    def apply(self, interface_changed=False, active="active"):
        calls = []

        def fake_run(*args, **kwargs):
            calls.append(args)
            return SimpleNamespace(stdout="stripped\n", returncode=0)

        with patch.object(remote, "run", side_effect=fake_run), \
             patch.object(remote, "unit_state", return_value=(active, "enabled")), \
             patch.object(remote.os, "fchown"):
            action = remote.apply_wireguard(self.text, interface_changed)
        return action, calls

    def test_adding_the_phone_key_to_a_running_interface_routes_the_phone(self):
        action, calls = self.apply()
        self.assertEqual(action, "peers synced")
        route = ("ip", "-4", "route", "replace", "10.77.77.2/32", "dev", "wg-simloc")
        self.assertIn(route, calls)
        self.assertGreater(calls.index(route), calls.index(("wg", "syncconf", "wg-simloc", "/dev/stdin")))

    def test_an_unchanged_config_still_repairs_a_missing_route(self):
        self.apply()
        action, calls = self.apply()
        self.assertIsNone(action)
        self.assertEqual(calls, [("ip", "-4", "route", "replace", "10.77.77.2/32", "dev", "wg-simloc")])

    def test_wg_quick_adds_routes_itself_when_it_starts_or_restarts(self):
        for kwargs in ({"active": "inactive"}, {"interface_changed": True}):
            _, calls = self.apply(**kwargs)
            self.assertFalse([c for c in calls if c[:1] == ("ip",)], kwargs)


class RemoteHelperTests(unittest.TestCase):
    def test_nft_rules_name_the_service_accounts_and_addresses(self):
        with patch.object(remote, "run"), patch.object(remote, "write_file", return_value=True) as write:
            self.assertTrue(remote.configure_nft(990, 991, "10.77.77.2", "10.77.77.11", 8765))
        text = write.call_args.args[1]
        self.assertIn("elements = { 0, 990, 991 }", text)
        self.assertIn("ip daddr 10.77.77.2 meta skuid != @allowed", text)
        self.assertIn("ip daddr 10.77.77.11 tcp dport 8765 meta skuid != 0", text)
        lines = [line for line in text.splitlines() if not line.startswith("#")]
        self.assertEqual(lines[:2], ["table inet simlocation", "delete table inet simlocation"],
                         "reload replaces only our table")
        self.assertIn('iifname "wg-simloc" counter drop', text)
        self.assertIn('oifname "wg-simloc" counter drop', text)

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


class AccountFileTests(unittest.TestCase):
    """Files below the service accounts' homes: links they plant must not steer root."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.top = Path(self.directory.name) / "home"
        self.top.mkdir()
        self.secret = Path(self.directory.name) / "secret"
        self.secret.write_text("root only")

    def tearDown(self):
        self.directory.cleanup()

    def test_read_private_refuses_links_at_any_depth(self):
        (self.top / "pairing").mkdir()
        (self.top / "pairing" / "device.plist").write_bytes(b"record")
        self.assertEqual(remote.read_private(self.top / "pairing" / "device.plist", self.top), b"record")
        self.assertIsNone(remote.read_private(self.top / "missing.json", self.top))
        self.assertIsNone(remote.read_private(self.top / "nodir" / "x", self.top))
        (self.top / "favorites.json").symlink_to(self.secret)
        with self.assertRaises(OSError):
            remote.read_private(self.top / "favorites.json", self.top)
        (self.top / "linked").symlink_to(self.secret.parent)
        with self.assertRaises(OSError):
            remote.read_private(self.top / "linked" / "secret", self.top)

    def test_write_file_refuses_a_link_and_leaves_no_temporary_file(self):
        target = self.top / "web.env"
        with patch.object(remote.os, "fchown"):
            self.assertTrue(remote.write_file(target, "A=1\n", 0o600))
            self.assertFalse(remote.write_file(target, "A=1\n", 0o600))
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
            link = self.top / "network.json"
            link.symlink_to(self.secret)
            with self.assertRaises(OSError):
                remote.write_file(link, "{}", 0o640)
        self.assertEqual(self.secret.read_text(), "root only")
        self.assertEqual(sorted(p.name for p in self.top.iterdir()), ["network.json", "web.env"])

    def test_account_write_script_is_atomic_and_private(self):
        target = self.top / "pairing" / "device.plist"
        for data in (b"first", b"second"):
            result = remote.subprocess.run([remote.sys.executable, "-I", "-c", remote.WRITE_AS_ACCOUNT,
                                            str(target), "600"], input=data, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(target.read_bytes(), b"second")
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        self.assertEqual([p.name for p in target.parent.iterdir()], ["device.plist"])

    def test_write_as_drops_to_the_account_and_skips_unchanged_content(self):
        target = self.top / "devices.json"
        target.write_text("{}")
        with patch.object(remote.subprocess, "run", return_value=SimpleNamespace(returncode=0)) as run:
            self.assertFalse(remote.write_as((990, 991), self.top, target, "{}"))
            run.assert_not_called()
            self.assertTrue(remote.write_as((990, 991), self.top, target, '{"a": 1}'))
        command = run.call_args.args[0]
        self.assertEqual(command[:5], ["setpriv", "--reuid=990", "--regid=991", "--clear-groups", "--inh-caps=-all"])
        self.assertEqual(command[-2:], [str(target), "600"])
        self.assertEqual(run.call_args.kwargs["input"], b'{"a": 1}')
        planted = self.top / "favorites.json"
        planted.symlink_to(self.secret)
        with patch.object(remote.subprocess, "run", return_value=SimpleNamespace(returncode=0)) as run:
            self.assertTrue(remote.write_as((990, 991), self.top, planted, "{}"), "a planted link never blocks a deploy")
        self.assertEqual(run.call_args.args[0][-2], str(planted))

    def test_chown_tree_never_follows_links(self):
        (self.top / "pairing").mkdir()
        (self.top / "pairing" / "device.plist").write_bytes(b"x")
        (self.top / "escape").symlink_to(self.secret.parent)
        (self.top / "shadow").symlink_to(self.secret)
        calls = []
        with patch.object(remote.os, "chown", side_effect=lambda *a, **k: calls.append((a[0], k))), \
             patch.object(remote, "ensure_dir") as ensure:
            remote.chown_tree(self.top, (990, 991))
        ensure.assert_called_once_with(self.top, 0o700, (990, 991))
        self.assertEqual(sorted(name for name, _ in calls), ["device.plist", "escape", "pairing", "shadow"])
        self.assertTrue(all(k["follow_symlinks"] is False and "dir_fd" in k for _, k in calls))


class PairRecordTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.local = Path(self.directory.name) / "remote.plist"
        self.keys = {"private_key": b"\x01" * 32, "public_key": b"\x02" * 32, "peer_alt_irk": b"\x03" * 16}
        self.local.write_bytes(plistlib.dumps(self.keys))
        self.inventory = {"phone": {"pair_record": str(self.local), "pair_reference": "one"}, "hosts": {}}

    def tearDown(self):
        self.directory.cleanup()

    def fetched(self, data):
        return patch.object(fleet, "remote", return_value={"record": base64.b64encode(data).decode()})

    def test_reference_copy_is_used_byte_for_byte_when_its_keys_match(self):
        pinned = plistlib.dumps({**self.keys, "host_identifier": "F00D"})
        with self.fetched(pinned):
            self.assertEqual(fleet.pair_record(self.inventory), pinned)

    def test_anything_else_from_the_reference_is_refused(self):
        foreign = plistlib.dumps({**self.keys, "private_key": b"\x09" * 32, "host_identifier": "F00D"})
        for data in (b"root:x:0:0::/root:/bin/bash\n", foreign, plistlib.dumps(self.keys)):
            with self.fetched(data), self.assertRaises(fleet.FleetError):
                fleet.pair_record(self.inventory)

    def test_without_a_reference_the_identifier_must_already_be_pinned(self):
        self.inventory["phone"]["pair_reference"] = None
        with self.assertRaises(fleet.FleetError):
            fleet.pair_record(self.inventory)
        self.local.write_bytes(plistlib.dumps({**self.keys, "host_identifier": "F00D"}))
        self.assertEqual(plistlib.loads(fleet.pair_record(self.inventory))["host_identifier"], "F00D")


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
        self.assertTrue({"bin/simlocation.py", "web/console.html", "web/login.html", "deploy/network_tunneld.py",
                         "web/vendor/leaflet-1.9.4/leaflet.js", "web/vendor/leaflet-1.9.4/images/marker-icon.png"} <= names)
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
            strict = (out / "SimLocation-strict.conf").read_text()
            modes = {path.name: stat.S_IMODE(path.stat().st_mode) for path in out.iterdir()}
        save.assert_called_once()
        self.assertTrue(inventory["phone"]["public_key"])
        self.assertNotIn("PrivateKey", json.dumps(inventory))
        self.assertEqual(combined.count("[Peer]"), 2)
        self.assertIn("AllowedIPs = 10.77.77.1/32", combined)
        self.assertIn("Endpoint = 203.0.113.9:51820", single)
        self.assertNotIn("0.0.0.0/0", combined)
        self.assertEqual(strict.count("[Peer]"), 2)
        self.assertEqual(strict.count("AllowedIPs = 0.0.0.0/0, ::/0"), 1)
        self.assertIn("# one\nPublicKey", strict.split("0.0.0.0/0")[0].rsplit("[Peer]", 1)[1])
        self.assertIn("AllowedIPs = 10.77.77.11/32", strict)
        self.assertEqual(strict.split("[Peer]")[0], combined.split("[Peer]")[0])
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
