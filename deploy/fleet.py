#!/usr/bin/env python3
"""Deploy and operate SimLocation on several VPS hosts from this Mac.

The inventory is deploy/fleet.json (git-ignored; template in
deploy/examples/fleet.json.example). Every remote step runs
`ssh HOST python3 -` with deploy/fleet_remote.py and the call's parameters on
stdin, so no secret is ever part of a command line. The phone's WireGuard key
and the console passwords are generated here and written only to a private
directory for you to scan or import; the servers keep public keys and hashes.

  fleet.py status [HOST ...]                    services, WireGuard, tunnel
  fleet.py deploy [HOST ...] [--restart-network]
  fleet.py phone [--out DIR]                    new phone key, configs, QR codes
  fleet.py password [HOST ...] [--out DIR]      new console passwords, CSV
  fleet.py favorites --from HOST [HOST ...]     copy saved places
"""

import argparse
import base64
import csv
import importlib.util
import io
import json
import os
import plistlib
import secrets
import subprocess
import sys
import tarfile
import tempfile
import threading
from pathlib import Path

DEPLOY = Path(__file__).resolve().parent
ROOT = DEPLOY.parent
INVENTORY = DEPLOY / "fleet.json"
REMOTE = DEPLOY / "fleet_remote.py"
LOCK = DEPLOY / "requirements.lock"
UNITS = DEPLOY / "systemd"
MARKER = "@@SIMLOCATION-RESULT@@"
BUNDLE = ["VERSION", "LICENSE", "bin/simlocation.py", "bin/simlocation_web.py", "deploy/README.md",
          "deploy/network_tunneld.py", "deploy/verify_device.py", "deploy/hold_probe.py"]
# Without 0/o and 1/l/i: four groups of six give about 119 bits.
PASSWORD_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"
PAIR_KEYS = ("private_key", "public_key")
DEFAULTS = {"wg_port": 51820, "web_port": 8765, "mtu": 1280, "pairing_port": 49152,
            "keepalive": {"idle": 30, "interval": 30, "count": 40}, "tcp_keepalive": "30,30,40",
            "web_user": "simlocation", "default_location": None, "persistent_keepalive": 25}


class FleetError(RuntimeError):
    pass


def note(message):
    print(message, file=sys.stderr, flush=True)


def load_inventory():
    if not INVENTORY.is_file():
        sys.exit(f"没有清单：把 deploy/examples/fleet.json.example 复制成 {INVENTORY} 后填写")
    inventory = json.loads(INVENTORY.read_text(encoding="utf-8"))
    inventory["defaults"] = {**DEFAULTS, **inventory.get("defaults", {})}
    return inventory


def save_inventory(inventory):
    data = {**inventory, "defaults": {key: value for key, value in inventory["defaults"].items()
                                      if DEFAULTS.get(key) != value or key == "default_location"}}
    temporary = INVENTORY.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(INVENTORY)


def select(inventory, names):
    unknown = [name for name in names if name not in inventory["hosts"]]
    if unknown:
        sys.exit("清单里没有：" + ", ".join(unknown))
    return names or list(inventory["hosts"])


def remote(inventory, name, function, params=None, timeout=2400):
    """Run one fleet_remote call on a host; its progress lines are echoed indented."""
    host = inventory["hosts"][name]
    payload = base64.b64encode(json.dumps(params or {}).encode("utf-8")).decode("ascii")
    script = (REMOTE.read_text(encoding="utf-8")
              + f"\nimport base64 as _b, json as _j\nmain({function!r}, _j.loads(_b.b64decode({payload!r})))\n")
    command = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=20", "-o", "ServerAliveInterval=15",
               "-o", "ServerAliveCountMax=8", "-o", "ControlMaster=no", "-o", "ControlPath=none",
               *host["ssh"], "python3", "-"]
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True)

    def pump():
        for line in process.stderr:
            note("    " + line.rstrip())

    reader = threading.Thread(target=pump, daemon=True)
    reader.start()
    timer = threading.Timer(timeout, process.kill)
    timer.start()
    try:
        process.stdin.write(script)
        process.stdin.close()
        output = process.stdout.read()
        process.wait()
    finally:
        timer.cancel()
        reader.join(5)
    for line in output.splitlines():
        if line.startswith(MARKER):
            result = json.loads(line[len(MARKER):])
            if not result["ok"]:
                raise FleetError(f"{name}: {result['error']}")
            return result["result"]
    raise FleetError(f"{name}: 没有结果（ssh 退出码 {process.returncode}）")


def endpoint(host):
    """The address the phone dials; "auto" reads it from the SSH config, so it stays out of files."""
    if host.get("endpoint", "auto") != "auto":
        return host["endpoint"]
    resolved = subprocess.run(["ssh", "-G", host["ssh"][-1]], capture_output=True, text=True, check=True).stdout
    for line in resolved.splitlines():
        if line.startswith("hostname "):
            return line.split(" ", 1)[1]
    raise FleetError(f"{host['ssh'][-1]}: ssh -G 没给出 hostname")


def build_bundle():
    names = BUNDLE + sorted(path.relative_to(ROOT).as_posix() for path in (ROOT / "web").rglob("*")
                            if path.is_file() and not any(part.startswith(".") for part in path.relative_to(ROOT).parts))
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name in names:
            info = archive.gettarinfo(str(ROOT / name), arcname=name)
            info.uid = info.gid = 0
            info.uname = info.gname = "root"
            info.mtime = 0
            with open(ROOT / name, "rb") as handle:
                archive.addfile(info, handle)
    git = ["git", "-C", str(ROOT)]
    revision = subprocess.run([*git, "rev-parse", "--short", "HEAD"], capture_output=True, text=True).stdout.strip()
    dirty = subprocess.run([*git, "status", "--porcelain", "--", *names], capture_output=True, text=True).stdout.strip()
    return base64.b64encode(buffer.getvalue()).decode("ascii"), (revision or "unknown") + ("-dirty" if dirty else "")


def parse_record(data):
    """A RemotePairing record as a dict, or None when data is anything else."""
    try:
        record = plistlib.loads(data)
    except Exception:
        return None
    if not isinstance(record, dict):
        return None
    if not all(isinstance(record.get(key), bytes) and len(record[key]) == 32 for key in PAIR_KEYS):
        return None
    return record


def pair_record(inventory):
    """The RemotePairing record every host presents, with its host identifier pinned.

    The pinned copy comes from a host where it is known to work. It is used
    byte for byte, and only after its keys match the record on this Mac, so a
    file swapped in on that host cannot be passed on to the others.
    """
    phone = inventory["phone"]
    local = parse_record(Path(phone["pair_record"]).expanduser().read_bytes())
    if local is None:
        raise FleetError(f"{phone['pair_record']} 不是有效的 RemotePairing 配对记录")
    reference = phone.get("pair_reference")
    if not reference:
        if "host_identifier" not in local:
            raise FleetError("配对记录里没有固定的 host_identifier；把清单里的 pair_reference 设为已经能连上手机的主机")
        return plistlib.dumps(local)
    data = base64.b64decode(remote(inventory, reference, "pair_get", timeout=120)["record"])
    record = parse_record(data)
    if record is None or "host_identifier" not in record or any(record[key] != local[key] for key in PAIR_KEYS):
        raise FleetError(f"{reference} 上的配对记录与本机的不是同一身份，已停止")
    return data


def private_dir(path):
    directory = Path(path).expanduser() if path else Path(tempfile.mkdtemp(prefix="simlocation-fleet-"))
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    return directory


def write_private(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb" if isinstance(data, bytes) else "w") as handle:
        handle.write(data)


def cmd_status(inventory, args):
    for name in select(inventory, args.hosts):
        try:
            state = remote(inventory, name, "status", timeout=120)
        except (FleetError, OSError) as exc:
            print(f"{name:13} 无法获取：{exc}")
            continue
        units = " ".join(f"{unit.replace('simlocation-', '').split('@')[0]}={active}/{enabled}"
                         for unit, (active, enabled) in state["units"].items())
        ages = [peer["handshake_age"] for peer in state["peers"] or [] if peer["handshake_age"] is not None]
        tunnel = {True: "已建立", False: "未建立", None: "tunneld 无响应"}[state["tunnel"]]
        handshake = f"{min(ages)}s 前" if ages else "无"
        sessions = ", ".join(f"{s['status']}{'' if s['alive'] else '(进程已退出)'}" for s in state["sessions"]) or "无"
        print(f"{name:13} 版本 {state['revision']}  pmd3 {state['pmd3']}  适配器账户 {state['network_user']}\n"
              f"{'':13} {units}\n"
              f"{'':13} WireGuard 握手 {handshake}  开发者通道 {tunnel}  网页 {state['web']}  "
              f"密码 {'已设置' if state['password_set'] else '未设置（一次性链接模式）'}  会话 {sessions}"
              + ("  ⚠ 服务器上仍有旧的手机私钥文件" if state.get("old_phone_key_file") else ""))


def cmd_deploy(inventory, args):
    names = select(inventory, args.hosts)
    defaults, phone = inventory["defaults"], inventory["phone"]
    bundle, revision = build_bundle()
    note(f"代码版本 {revision}")
    record = base64.b64encode(pair_record(inventory)).decode("ascii")
    units = {path.name: path.read_text(encoding="utf-8") for path in sorted(UNITS.glob("*.service"))}
    auto_host = auto_set_host(inventory)
    if auto_host:
        note(f"连上 WireGuard 后自动设到默认位置：由 {auto_host} 负责")
    failures = []
    for name in names:
        host = inventory["hosts"][name]
        note(f"== {name}")
        params = {key: defaults[key] for key in ("wg_port", "web_port", "mtu", "pairing_port", "keepalive",
                                                 "tcp_keepalive", "web_user", "default_location")}
        params.update(address=host["address"], phone=phone["address"], label=host.get("label", name),
                      bundle=bundle, revision=revision, requirements=LOCK.read_text(encoding="utf-8"),
                      units=units, pair_record=record, udid=phone["udid"], alias=phone["alias"],
                      phone_key=phone.get("public_key"), restart_network=args.restart_network,
                      auto_set=name == auto_host)
        try:
            report = remote(inventory, name, "deploy", params)
        except (FleetError, OSError) as exc:
            failures.append(name)
            print(f"{name}: 部署失败：{exc}")
            continue
        host["server_public_key"] = report["server_public_key"]
        save_inventory(inventory)
        print(f"{name}: 完成。新装 {', '.join(report['packages']) or '无'}；代码改动 {report['code_changed']} 个文件；"
              f"重启 {', '.join(report['restarts']) or '无'}；开发者镜像预下载 {'成功' if report['developer_image_cached'] else '失败'}"
              + "".join(f"\n  待办：{item}" for item in report["pending"]))
    if failures:
        sys.exit(1)


# The strict profile sends every route into the tunnel. One peer must own the
# default routes (WireGuard allows each prefix on one peer only); the others keep
# their /32, which is more specific. That host drops forwarded traffic, so other
# apps get no network instead of leaving outside the tunnel while Surge is off.
FULL_TUNNEL = "0.0.0.0/0, ::/0"


def peer_block(peer, keepalive, allowed=None):
    return (f"[Peer]\n# {peer['name']}\nPublicKey = {peer['public_key']}\nEndpoint = {peer['endpoint']}\n"
            f"AllowedIPs = {allowed or peer['address'] + '/32'}\nPersistentKeepalive = {keepalive}\n")


def auto_set_host(inventory):
    """The one host whose console sets the default point when the phone connects.

    phone.auto_set_host names it; null or "" turns the feature off. Several
    hosts would each start their own session and fight over the location.
    """
    phone = inventory["phone"]
    if "auto_set_host" not in phone:
        return sink_host(inventory) if inventory["defaults"].get("default_location") else None
    chosen = phone["auto_set_host"]
    if chosen and chosen not in inventory["hosts"]:
        raise FleetError(f"phone.auto_set_host 指向未知主机 {chosen}")
    return chosen or None


def sink_host(inventory):
    reference = inventory["phone"].get("pair_reference")
    return reference if reference in inventory["hosts"] else next(iter(inventory["hosts"]))


def cmd_phone(inventory, args):
    import qrcode
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

    defaults, phone = inventory["defaults"], inventory["phone"]
    key = X25519PrivateKey.generate()
    raw = serialization.Encoding.Raw
    private = base64.b64encode(key.private_bytes(raw, serialization.PrivateFormat.Raw,
                                                 serialization.NoEncryption())).decode("ascii")
    public = base64.b64encode(key.public_key().public_bytes(raw, serialization.PublicFormat.Raw)).decode("ascii")
    peers = []
    for name, host in inventory["hosts"].items():
        note(f"== {name}: 换上新的手机公钥")
        info = remote(inventory, name, "set_phone_key", {"public_key": public, "phone": phone["address"]}, timeout=180)
        host["server_public_key"] = info["server_public_key"]
        peers.append({"name": name, "public_key": info["server_public_key"], "address": info["address"],
                      "endpoint": f"{endpoint(host)}:{info['listen_port']}"})
    phone["public_key"] = public
    save_inventory(inventory)

    out = private_dir(args.out)
    interface = f"[Interface]\nPrivateKey = {private}\nAddress = {phone['address']}/32\nMTU = {defaults['mtu']}\n"
    sink = sink_host(inventory)
    configs = {"SimLocation-all": (peers, None), "SimLocation-strict": (peers, sink),
               **{f"SimLocation-{peer['name']}": ([peer], None) for peer in peers}}
    for title, (chosen, full) in configs.items():
        text = interface + "".join(
            "\n" + peer_block(peer, defaults["persistent_keepalive"], FULL_TUNNEL if peer["name"] == full else None)
            for peer in chosen)
        write_private(out / f"{title}.conf", text)
        image = io.BytesIO()
        qrcode.make(text, error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=8, border=4).save(image, "PNG")
        write_private(out / f"{title}.png", image.getvalue())
    write_private(out / "README.txt",
                  "SimLocation-all：一个隧道连全部 VPS，推荐导入。只有 SimLocation 的流量进隧道，其他 App 照常上网（Surge 停用期间不受保护）。\n"
                  f"SimLocation-strict：同一把密钥，所有流量进隧道，由 {sink} 丢弃；开着时只有 SimLocation 能用，其他 App 断网。"
                  "和 SimLocation-all 只开一个。控制台的地图由 VPS 代取，照常可用。\n"
                  "SimLocation-<主机>：只连一台，备用；iOS 同一时间只能开一个 VPN。\n"
                  "这些文件含手机私钥。扫码导入后请删除整个文件夹。\n")
    print(f"手机配置和二维码在 {out}")
    if sys.platform == "darwin" and not args.no_open:
        subprocess.run(["open", str(out / "SimLocation-all.png")], check=False)


def load_web_module():
    spec = importlib.util.spec_from_file_location("simlocation_web", ROOT / "bin" / "simlocation_web.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def cmd_password(inventory, args):
    web = load_web_module()
    defaults = inventory["defaults"]
    out = private_dir(args.out)
    rows = [["Title", "URL", "Username", "Password", "Notes", "OTPAuth"]]
    for name in select(inventory, args.hosts):
        host = inventory["hosts"][name]
        password = "-".join("".join(secrets.choice(PASSWORD_ALPHABET) for _ in range(6)) for _ in range(4))
        remote(inventory, name, "set_password", {"password_hash": web.hash_password(password)}, timeout=180)
        rows.append([f"SimLocation · {host.get('label', name)}", f"http://{host['address']}:{defaults['web_port']}/",
                     defaults["web_user"], password, "SimLocation 控制台，只能在 WireGuard 内打开", ""])
        print(f"{name}: 新密码已生效（旧会话全部退出）")
    buffer = io.StringIO()
    csv.writer(buffer).writerows(rows)
    path = out / "simlocation-passwords.csv"
    write_private(path, buffer.getvalue())
    print(f"密码表在 {path}：在「密码」App 里用 文件 → 导入密码 导入，导入后删除这个文件。")


def cmd_favorites(inventory, args):
    favorites = remote(inventory, args.source, "favorites_get", timeout=120)["favorites"]
    if favorites is None:
        sys.exit(f"{args.source} 上还没有收藏")
    for name in select(inventory, args.hosts):
        if name != args.source:
            count = remote(inventory, name, "favorites_put", {"favorites": favorites}, timeout=120)["items"]
            print(f"{name}: 写入 {count} 个收藏")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("status", "deploy", "password"):
        sub_parser = sub.add_parser(command)
        sub_parser.add_argument("hosts", nargs="*")
    sub.choices["deploy"].add_argument("--restart-network", action="store_true",
                                       help="重启开发者通道服务，即使通道正在使用（手机须在 Wi-Fi 下才能重建）")
    sub.choices["password"].add_argument("--out", help="密码表输出目录（默认新建私有临时目录）")
    phone = sub.add_parser("phone")
    phone.add_argument("--out", help="配置输出目录（默认新建私有临时目录）")
    phone.add_argument("--no-open", action="store_true")
    favorites = sub.add_parser("favorites")
    favorites.add_argument("--from", dest="source", required=True)
    favorites.add_argument("hosts", nargs="*")
    args = parser.parse_args(argv)
    inventory = load_inventory()
    handler = {"status": cmd_status, "deploy": cmd_deploy, "phone": cmd_phone,
               "password": cmd_password, "favorites": cmd_favorites}[args.command]
    try:
        handler(inventory, args)
    except FleetError as exc:
        sys.exit(str(exc))


if __name__ == "__main__":
    main()
