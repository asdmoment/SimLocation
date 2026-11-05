"""Offline tests using only temporary, freshly generated WireGuard identities."""

import base64
import copy
import importlib.util
import ipaddress
from pathlib import Path
import stat
import tempfile
import traceback
import unittest

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey


_SPEC = importlib.util.spec_from_file_location("surge_export", Path(__file__).with_name("surge_export.py"))
surge_export = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(surge_export)


def keypair():
    key = X25519PrivateKey.generate()
    private = key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                                serialization.NoEncryption())
    public = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return base64.b64encode(private).decode("ascii"), base64.b64encode(public).decode("ascii")


class SurgeExportTests(unittest.TestCase):
    def setUp(self):
        self.private, self.public = keypair()
        self.psk, _ = keypair()
        self.inventory = {
            "phone": {"address": "10.77.0.2", "public_key": self.public},
            "defaults": {"mtu": 1280, "persistent_keepalive": 25},
            "hosts": {},
        }
        self.peers = []
        self.endpoints = {}
        for index in range(4):
            name = f"server-{index}"
            _, public = keypair()
            address = f"10.77.0.{index + 10}"
            endpoint = f"wg{index}.example.org:51820"
            self.inventory["hosts"][name] = {
                "address": address, "server_public_key": public,
                "ssh": [f"test-{index}"], "endpoint": "auto",
            }
            self.endpoints[name] = endpoint
            self.peers.append({"PublicKey": public, "AllowedIPs": f"{address}/32",
                               "Endpoint": endpoint, "PersistentKeepalive": "25"})

    def source(self, private=None, address="10.77.0.2/24", peers=None):
        lines = ["# Official WireGuard export", "[Interface]",
                 f"PrivateKey = {self.private if private is None else private}",
                 f"Address = {address}", "DNS = 1.1.1.1", "MTU = 1420"]
        for peer in self.peers if peers is None else peers:
            lines.extend(["", "[Peer]"])
            lines.extend(f"{key} = {value}" for key, value in peer.items())
        return "\n".join(lines) + "\n"

    def render(self, **kwargs):
        return surge_export.render(kwargs.pop("inventory", self.inventory),
                                   kwargs.pop("source", self.source()),
                                   kwargs.pop("endpoints", None))

    def assert_invalid(self, **kwargs):
        with self.assertRaises(surge_export.ExportError) as caught:
            self.render(**kwargs)
        error = "".join(traceback.format_exception(caught.exception))
        secrets = [self.private, self.public, self.psk]
        secrets.extend(host["server_public_key"] for host in self.inventory["hosts"].values())
        for secret in secrets:
            self.assertNotIn(secret, error)
        return str(caught.exception)

    def test_four_peers_single_policy_and_exact_rules(self):
        rendered = self.render()
        self.assertEqual(rendered.count(" = wireguard, section-name="), 1)
        self.assertEqual(rendered.count("[WireGuard simlocation-fleet]"), 1)
        self.assertEqual(rendered.count("peer = ("), 4)
        self.assertEqual(rendered.count("private-key = "), 1)
        self.assertIn(f"private-key = {self.private}\n", rendered)
        self.assertIn("self-ip = 10.77.0.2\nmtu = 1280\n", rendered)
        self.assertEqual(rendered.count("keepalive = 25"), 4)
        self.assertEqual([line for line in rendered.splitlines() if line.startswith("IP-CIDR,")],
                         [f"IP-CIDR,{host['address']}/32,SimLocation-WG,no-resolve"
                          for host in self.inventory["hosts"].values()])
        for forbidden in ("dns-server", "allow-peer-access", "listen-port", "underlying-proxy", "1.1.1.1"):
            self.assertNotIn(forbidden, rendered)

    def test_defaults_without_mutation(self):
        del self.inventory["defaults"]
        original = copy.deepcopy(self.inventory)
        rendered = self.render()
        self.assertIn("mtu = 1280", rendered)
        self.assertEqual(self.inventory, original)

    def test_wrong_private_key(self):
        private, _ = keypair()
        message = self.assert_invalid(source=self.source(private=private))
        self.assertNotIn(private, message)

    def test_wrong_phone_address(self):
        self.assert_invalid(source=self.source(address="10.77.0.99/24"))

    def test_multiple_phone_addresses(self):
        self.assert_invalid(source=self.source(address="10.77.0.2/32, 10.77.0.3/32"))

    def test_missing_peer(self):
        self.assert_invalid(source=self.source(peers=self.peers[:-1]))

    def test_extra_unknown_peer(self):
        extra = dict(self.peers[0], PublicKey=keypair()[1])
        self.assert_invalid(source=self.source(peers=self.peers + [extra]))

    def test_replaced_unknown_peer(self):
        self.peers[0]["PublicKey"] = keypair()[1]
        self.assert_invalid()

    def test_duplicate_peer_key(self):
        self.peers[1]["PublicKey"] = self.peers[0]["PublicKey"]
        self.assert_invalid()

    def test_duplicate_server_address(self):
        self.inventory["hosts"]["server-1"]["address"] = "10.77.0.10"
        self.assert_invalid()

    def test_duplicate_inventory_key(self):
        self.inventory["hosts"]["server-1"]["server_public_key"] = self.peers[0]["PublicKey"]
        self.assert_invalid()

    def test_server_address_collides_with_phone(self):
        self.inventory["hosts"]["server-0"]["address"] = "10.77.0.2"
        self.assert_invalid()

    def test_incomplete_host(self):
        for field in ("address", "server_public_key", "ssh", "endpoint"):
            with self.subTest(field=field):
                inventory = copy.deepcopy(self.inventory)
                del inventory["hosts"]["server-0"][field]
                self.assert_invalid(inventory=inventory)

    def test_invalid_host_names_and_injection(self):
        for name in ("bad\n[Rule]", "bad,name", "bad name", "bad=policy", "", "a" * 65):
            with self.subTest(name=name):
                inventory = copy.deepcopy(self.inventory)
                inventory["hosts"][name] = inventory["hosts"].pop("server-0")
                self.assert_invalid(inventory=inventory)

    def test_default_routes_are_narrowed(self):
        for peer in self.peers:
            peer["AllowedIPs"] = "0.0.0.0/0, ::/0"
        rendered = self.render()
        self.assertNotIn("0.0.0.0/0", rendered)
        self.assertNotIn("::/0", rendered)
        allowed = [line.split("allowed-ips = ", 1)[1].split(",", 1)[0]
                   for line in rendered.splitlines() if line.startswith("peer = ")]
        self.assertEqual(len(allowed), 4)
        self.assertTrue(all(ipaddress.ip_network(value).prefixlen == 32 for value in allowed))

    def test_allowed_ips_must_cover_server(self):
        self.peers[0]["AllowedIPs"] = "192.0.2.1/32"
        self.assert_invalid()

    def test_invalid_allowed_ips(self):
        self.peers[0]["AllowedIPs"] = "garbage"
        self.assert_invalid()

    def test_psk_preserved(self):
        self.peers[0]["PresharedKey"] = self.psk
        self.assertIn(f"preshared-key = {self.psk}", self.render())

    def test_invalid_keys_never_leak(self):
        invalid = ("not-a-key", base64.b64encode(b"short").decode("ascii"), self.psk + "\nInjected")
        for key in invalid:
            with self.subTest(key_length=len(key)):
                self.assert_invalid(source=self.source(private=key))
                self.peers[0]["PresharedKey"] = key
                self.assert_invalid()
        for field in ("public_key",):
            inventory = copy.deepcopy(self.inventory)
            inventory["phone"][field] = "bad"
            self.assert_invalid(inventory=inventory)

    def test_malformed_endpoint_rejected(self):
        invalid = ("example.org", "example.org:0", "example.org:65536", "example.org:-1",
                   "example.org:51820, keepalive = 0", "example.org:51820\n[Rule]",
                   "example.org:51820\rInjected = yes", "bad host:123", "a/b:123",
                   "-bad.example:123", "256.0.0.1:123", "[not-ip]:123", "::1:123",
                   "[fe80::1%en0]:123", "foo..bar:123", "good.example\x00:123")
        for endpoint in invalid:
            with self.subTest(endpoint=endpoint):
                self.peers[0]["Endpoint"] = endpoint
                self.assert_invalid()

    def test_ipv4_ipv6_dns_endpoints(self):
        for endpoint in ("192.0.2.4:51820", "[2001:db8::1]:51820", "WG.Example.ORG.:51820"):
            with self.subTest(endpoint=endpoint):
                self.peers[0]["Endpoint"] = endpoint
                self.assertIn("endpoint = ", self.render())

    def test_endpoint_mapping_agrees_with_source(self):
        self.assertEqual(self.render(), self.render(endpoints=self.endpoints))
        self.endpoints["server-0"] = "different.example.org:51820"
        self.assert_invalid(endpoints=self.endpoints)

    def test_endpoint_mapping_fills_missing_source(self):
        for peer in self.peers:
            del peer["Endpoint"]
        self.assertEqual(self.render(endpoints=self.endpoints).count("endpoint = "), 4)
        self.assert_invalid()

    def test_endpoint_mapping_must_be_complete_and_safe(self):
        incomplete = dict(self.endpoints)
        del incomplete["server-0"]
        self.assert_invalid(endpoints=incomplete)
        self.assert_invalid(endpoints={**self.endpoints, "extra": "extra.example.org:123"})
        self.assert_invalid(endpoints={**self.endpoints, "server-0": "host:123, injected=yes"})

    def test_peer_order_does_not_change_identity(self):
        rendered = self.render()
        self.peers.reverse()
        self.assertEqual(rendered, self.render())

    def test_duplicate_interface_and_fields_rejected(self):
        self.assert_invalid(source=self.source() + "[Interface]\nPrivateKey = ignored\n")
        self.assert_invalid(source=self.source() + "PublicKey = ignored\n")

    def test_unknown_source_field_rejected(self):
        self.assert_invalid(source=self.source().replace("MTU = 1420", "PostUp = arbitrary-command"))

    def test_crlf_export(self):
        self.assertEqual(self.render(), self.render(source=self.source().replace("\n", "\r\n")))

    def test_custom_defaults_validated(self):
        self.inventory["defaults"] = {"mtu": 1400, "persistent_keepalive": 0}
        self.assertIn("mtu = 1400", self.render())
        self.assertEqual(self.render().count("keepalive = 0"), 4)
        for defaults in ({"mtu": "1280"}, {"mtu": True}, {"mtu": 0},
                         {"persistent_keepalive": -1}, {"persistent_keepalive": 65536}):
            self.inventory["defaults"] = defaults
            self.assert_invalid()

    def test_malformed_inventory_rejected(self):
        for inventory in (None, {}, {"phone": [], "hosts": {}},
                          {**self.inventory, "defaults": []},
                          {**self.inventory, "hosts": list(self.inventory["hosts"].values())}):
            self.assert_invalid(inventory=inventory)

    def test_export_bundle_permissions_and_no_overwrite(self):
        # macOS /var may itself be a symlink; use a canonical fixture parent.
        with tempfile.TemporaryDirectory(dir=Path(tempfile.gettempdir()).resolve()) as temporary:
            output = Path(temporary) / "new-export"
            returned = surge_export.export_bundle(self.inventory, self.source(), None, output)
            self.assertEqual(returned, output)
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o700)
            self.assertEqual({file.name for file in output.iterdir()}, {"simlocation-surge.conf", "README.txt"})
            for file in output.iterdir():
                self.assertEqual(stat.S_IMODE(file.stat().st_mode), 0o600)
            self.assertEqual((output / "simlocation-surge.conf").read_text(), self.render())
            readme = (output / "README.txt").read_text()
            self.assertIn("BEFORE LAN/private-network DIRECT", readme)
            self.assertIn("another simultaneously active device", readme)
            with self.assertRaises(surge_export.ExportError):
                surge_export.export_bundle(self.inventory, self.source(), None, output)
            self.assertEqual((output / "simlocation-surge.conf").read_text(), self.render())

    def test_export_bundle_refuses_symlink_components(self):
        with tempfile.TemporaryDirectory(dir=Path(tempfile.gettempdir()).resolve()) as temporary:
            parent = Path(temporary)
            real = parent / "real"
            real.mkdir()
            link = parent / "link"
            link.symlink_to(real, target_is_directory=True)
            for output in (link, link / "new-export"):
                with self.subTest(output_name=output.name):
                    with self.assertRaises(surge_export.ExportError):
                        surge_export.export_bundle(self.inventory, self.source(), None, output)
            self.assertEqual(list(real.iterdir()), [])

    def test_invalid_source_creates_no_output(self):
        with tempfile.TemporaryDirectory(dir=Path(tempfile.gettempdir()).resolve()) as temporary:
            output = Path(temporary) / "invalid-export"
            with self.assertRaises(surge_export.ExportError):
                surge_export.export_bundle(self.inventory, self.source(peers=[]), None, output)
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
