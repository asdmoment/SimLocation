"""Validate an official four-peer WireGuard export and render a Surge fragment.

No inventory loading, network access, key generation, or fleet mutations occur here.
Call render() with an already loaded inventory; endpoints, when supplied, maps every
inventory host name to a complete HOST:PORT (IPv6 uses [HOST]:PORT).
"""

import base64
import binascii
from collections.abc import Mapping
import ipaddress
import os
from pathlib import Path
import re

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey


class ExportError(ValueError):
    """A validation failure whose message never includes source data or keys."""


_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")
_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\Z")
_FIELDS = {
    "interface": {"privatekey", "address", "dns", "mtu", "listenport"},
    "peer": {"publickey", "presharedkey", "allowedips", "endpoint", "persistentkeepalive"},
}
_README = """SimLocation Surge profile fragment

Merge the Proxy, WireGuard and Rule sections into your existing Surge profile.
Do not replace your entire profile with this fragment. Put these four IP-CIDR
rules BEFORE LAN/private-network DIRECT rules. Only the four server /32 routes
are exported; the source's default routes and DNS settings are not imported.

On iOS, enable either official WireGuard or Surge, never both together.
Do not import this same private key into another simultaneously active device.
This is a profile fragment, not a Surge module. Keep these files private: the
.conf contains the phone's private key and may also contain preshared keys.
Export an explicit .conf from official WireGuard; ZIP archives are unsupported.
"""


def _key(value):
    if not isinstance(value, str):
        raise ExportError("密钥必须是 32 字节的标准 Base64")
    try:
        raw = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error):
        raise ExportError("密钥必须是 32 字节的标准 Base64") from None
    if len(raw) != 32 or base64.b64encode(raw).decode("ascii") != value:
        raise ExportError("密钥必须是 32 字节的标准 Base64")
    return raw


def _ipv4(value):
    try:
        if not isinstance(value, str):
            raise ValueError
        return str(ipaddress.IPv4Address(value))
    except ValueError:
        raise ExportError("清单地址必须是单个 IPv4 地址") from None


def _integer(value, minimum, maximum):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ExportError("清单数值参数不合法")
    return value


def _hostname(value):
    if not isinstance(value, str) or not value or "%" in value:
        raise ExportError("endpoint 主机地址不合法")
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        pass
    # Reject malformed numeric IPv4 literals rather than treating them as DNS.
    if re.fullmatch(r"[0-9.]+", value):
        raise ExportError("endpoint 主机地址不合法")
    name = value[:-1] if value.endswith(".") else value
    if len(name) > 253 or not all(_LABEL.fullmatch(label) for label in name.split(".")):
        raise ExportError("endpoint 主机地址不合法")
    return name.lower()


def _endpoint(value):
    if not isinstance(value, str):
        raise ExportError("endpoint 必须包含主机和端口")
    if value.startswith("["):
        match = re.fullmatch(r"\[([^\[\]]+)\]:([0-9]{1,5})", value)
        if not match:
            raise ExportError("endpoint 格式不合法")
        host, port = match.groups()
        try:
            host = str(ipaddress.IPv6Address(host))
        except ValueError:
            raise ExportError("endpoint IPv6 地址不合法") from None
        if "%" in host:
            raise ExportError("endpoint IPv6 地址不合法")
        host = f"[{host}]"
    else:
        match = re.fullmatch(r"([^:]+):([0-9]{1,5})", value)
        if not match:
            raise ExportError("endpoint 格式不合法")
        host, port = match.groups()
        host = _hostname(host)
    port = _integer(int(port), 1, 65535)
    return f"{host}:{port}"


def _parse(text):
    if not isinstance(text, str) or any(ord(c) < 32 and c not in "\r\n\t" for c in text):
        raise ExportError("WireGuard 配置文本不合法；请导出明确的 .conf 文件，不支持 ZIP")
    interface = None
    peers = []
    current = None
    section = None
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        if line.lower() in ("[interface]", "[peer]"):
            section = line[1:-1].lower()
            current = {}
            if section == "interface":
                if interface is not None or peers:
                    raise ExportError("WireGuard Interface 必须唯一且位于 Peer 之前")
                interface = current
            else:
                if interface is None:
                    raise ExportError("WireGuard 配置缺少 Interface")
                peers.append(current)
            continue
        if current is None or "=" not in line:
            raise ExportError("WireGuard 配置包含不支持的行或节")
        field, value = line.split("=", 1)
        field = field.strip().lower()
        if field not in _FIELDS[section] or field in current:
            raise ExportError("WireGuard 配置包含未知或重复字段")
        current[field] = value.strip()
    if interface is None or not {"privatekey", "address"} <= interface.keys():
        raise ExportError("WireGuard Interface 不完整")
    return interface, peers


def render(inventory, exported_wg_text, endpoints=None):
    """Return one complete Surge profile fragment containing exactly four peers.

    Source peer identity is pinned to inventory public keys. Explicit endpoints
    must cover exactly the same host names, and agree with source endpoints if
    present. No endpoint resolution (including fleet.endpoint / ssh -G) is done.
    """
    if not isinstance(inventory, Mapping):
        raise ExportError("清单不合法")
    phone = inventory.get("phone")
    hosts = inventory.get("hosts")
    defaults = inventory.get("defaults", {})
    if not isinstance(phone, Mapping) or not isinstance(defaults, Mapping):
        raise ExportError("清单手机或默认配置不完整")
    if not isinstance(hosts, Mapping) or len(hosts) != 4:
        raise ExportError("清单必须恰好包含四台服务器")
    phone_ip = _ipv4(phone.get("address"))
    phone_key = _key(phone.get("public_key"))
    mtu = _integer(defaults.get("mtu", 1280), 576, 1420)
    keepalive = _integer(defaults.get("persistent_keepalive", 25), 0, 65535)
    if endpoints is not None and (not isinstance(endpoints, Mapping) or set(endpoints) != set(hosts)):
        raise ExportError("endpoint 映射必须完整对应四台服务器")
    addresses = {phone_ip}
    keys = {phone_key}
    validated = []
    for name, host in hosts.items():
        if not isinstance(name, str) or not _NAME.fullmatch(name):
            raise ExportError("服务器名称不合法")
        if not isinstance(host, Mapping) or not {"address", "server_public_key", "ssh", "endpoint"} <= host.keys():
            raise ExportError("服务器清单不完整")
        if "name" in host and host["name"] != name:
            raise ExportError("服务器名称不一致")
        ssh = host["ssh"]
        if not isinstance(ssh, list) or not ssh or any(
            not isinstance(item, str) or not item or any(ord(c) < 32 or ord(c) == 127 for c in item)
            for item in ssh
        ):
            raise ExportError("服务器 SSH 定义不完整")
        if host["endpoint"] != "auto":
            _hostname(host["endpoint"])
        address = _ipv4(host["address"])
        key = _key(host["server_public_key"])
        if address in addresses or key in keys:
            raise ExportError("清单存在重复服务器地址或密钥")
        addresses.add(address)
        keys.add(key)
        validated.append((name, address, key, host["server_public_key"]))

    interface, peers = _parse(exported_wg_text)
    private = _key(interface["privatekey"])
    public = X25519PrivateKey.from_private_bytes(private).public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    if public != phone_key:
        raise ExportError("导出私钥与清单手机公钥不匹配")
    try:
        source_address = ipaddress.IPv4Interface(interface["address"])
    except ValueError:
        raise ExportError("导出 Interface 必须只有一个 IPv4 地址") from None
    if str(source_address.ip) != phone_ip:
        raise ExportError("导出 Interface 地址与清单不匹配")
    if len(peers) != 4:
        raise ExportError("导出必须恰好包含四个 Peer，不得缺失或附加未知 Peer")
    by_key = {}
    for peer in peers:
        if not {"publickey", "allowedips"} <= peer.keys():
            raise ExportError("导出 Peer 不完整")
        key = _key(peer["publickey"])
        if key not in keys - {phone_key}:
            raise ExportError("导出包含未知 Peer 公钥")
        if key in by_key:
            raise ExportError("导出包含重复 Peer 公钥")
        if "presharedkey" in peer:
            _key(peer["presharedkey"])
        by_key[key] = peer

    lines = ["[Proxy]", "SimLocation-WG = wireguard, section-name=simlocation-fleet", "",
             "[WireGuard simlocation-fleet]", f"private-key = {interface['privatekey']}",
             f"self-ip = {phone_ip}", f"mtu = {mtu}"]
    for name, address, key, encoded_key in validated:
        peer = by_key[key]
        try:
            networks = [ipaddress.ip_network(value.strip(), strict=False)
                        for value in peer["allowedips"].split(",")]
        except ValueError:
            raise ExportError("导出 Peer 的 AllowedIPs 不合法") from None
        if not any(ipaddress.IPv4Address(address) in network for network in networks):
            raise ExportError("导出 Peer 的 AllowedIPs 未包含对应服务器")
        source = _endpoint(peer["endpoint"]) if "endpoint" in peer else None
        supplied = _endpoint(endpoints[name]) if endpoints is not None else None
        if source is not None and supplied is not None and source != supplied:
            raise ExportError("endpoint 映射与导出 Peer 不一致")
        endpoint = supplied or source
        if endpoint is None:
            raise ExportError("导出 Peer 缺少 endpoint，需提供完整映射")
        fields = [f"public-key = {encoded_key}", f"allowed-ips = {address}/32",
                  f"endpoint = {endpoint}", f"keepalive = {keepalive}"]
        if "presharedkey" in peer:
            fields.append(f"preshared-key = {peer['presharedkey']}")
        lines.append("peer = (" + ", ".join(fields) + ")")
    lines.extend(["", "[Rule]"])
    lines.extend(f"IP-CIDR,{address}/32,SimLocation-WG,no-resolve" for _, address, _, _ in validated)
    return "\n".join(lines) + "\n"


def export_bundle(inventory, exported_wg_text, endpoints, output_dir):
    """Write validated .conf and README in a new 0700 directory, files 0600.

    Every path component is opened without following symlinks. The parent must
    exist, and an existing output directory (including a symlink) is refused.
    Errors deliberately do not echo filesystem paths or configuration data.
    """
    text = render(inventory, exported_wg_text, endpoints)
    path = Path(os.path.abspath(output_dir))
    parent_fd = None
    directory_fd = None
    created = False
    written = []
    try:
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        parent_fd = os.open(path.anchor, flags)
        for part in path.parts[1:-1]:
            next_fd = os.open(part, flags, dir_fd=parent_fd)
            os.close(parent_fd)
            parent_fd = next_fd
        os.mkdir(path.name, 0o700, dir_fd=parent_fd)
        created = True
        directory_fd = os.open(path.name, flags, dir_fd=parent_fd)
        os.fchmod(directory_fd, 0o700)
        for name, content in (("simlocation-surge.conf", text), ("README.txt", _README)):
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=directory_fd)
            written.append(name)
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                os.fchmod(handle.fileno(), 0o600)
                handle.write(content)
    except OSError:
        if directory_fd is not None:
            for name in written:
                try:
                    os.unlink(name, dir_fd=directory_fd)
                except OSError:
                    pass
        if created:
            try:
                os.rmdir(path.name, dir_fd=parent_fd)
            except OSError:
                pass
        raise ExportError("无法创建私密导出目录；父目录必须存在且不能含符号链接，输出目录必须为新目录") from None
    finally:
        if directory_fd is not None:
            os.close(directory_fd)
        if parent_fd is not None:
            os.close(parent_fd)
    return path
