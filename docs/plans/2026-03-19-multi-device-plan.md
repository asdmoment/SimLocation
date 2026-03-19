# Multi-Device Location Management Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Support independent simultaneous location sessions for multiple iOS devices, with aliases, default device, and status management.

**Architecture:** Per-device file isolation (`var/<UDID>.pid`, `var/<UDID>.state.json`) with a `var/devices.json` for aliases and default device config. Device resolution follows a priority chain: `--device` flag → `SIMLOCATION_UDID` env → `devices.json` default → auto-discover. Core DVT session logic unchanged.

**Tech Stack:** Python 3, argparse, pymobiledevice3, requests

---

### Task 1: devices.json CRUD helpers

**Files:**
- Modify: `bin/simlocation.py` (add after `HOLD_POLL_INTERVAL_SECONDS` constant, ~line 63)

**Step 1: Add constant and helper functions**

After the existing constants block (~line 63), add:

```python
DEFAULT_DEVICES_PATH = RUNTIME_DIR / "devices.json"


def read_devices(devices_path=DEFAULT_DEVICES_PATH):
    if not devices_path.exists():
        return {"default": None, "aliases": {}}
    try:
        data = json.loads(devices_path.read_text(encoding="utf-8"))
        if "aliases" not in data:
            data["aliases"] = {}
        if "default" not in data:
            data["default"] = None
        return data
    except (OSError, json.JSONDecodeError):
        return {"default": None, "aliases": {}}


def write_devices(data, devices_path=DEFAULT_DEVICES_PATH):
    devices_path.parent.mkdir(parents=True, exist_ok=True)
    devices_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def resolve_alias(name, devices_data):
    """Resolve an alias or UDID string to a UDID. Returns the input unchanged if not an alias."""
    return devices_data["aliases"].get(name, name)


def reverse_alias(udid, devices_data):
    """Find the alias for a UDID, or return None."""
    for alias, u in devices_data["aliases"].items():
        if u == udid:
            return alias
    return None
```

**Step 2: Add per-device path helpers**

```python
def pid_path_for(udid):
    return RUNTIME_DIR / f"{udid}.pid"


def state_path_for(udid):
    return RUNTIME_DIR / f"{udid}.state.json"
```

**Step 3: Verify syntax**

Run: `python3 -m py_compile bin/simlocation.py`
Expected: no output (success)

**Step 4: Commit**

```bash
git add bin/simlocation.py
git commit -m "feat: add devices.json CRUD helpers and per-device path functions"
```

---

### Task 2: Multi-device resolve_device_udid

**Files:**
- Modify: `bin/simlocation.py` — rewrite `resolve_device_udid()` (~line 278) and add `discover_devices()`, `interactive_device_select()`

**Step 1: Add device discovery function**

Before `resolve_device_udid`, add:

```python
def discover_devices(pmd3_bin, log_path=None):
    """Return a list of UDIDs from tunneld and/or lockdown."""
    udids = set()
    try:
        data = get_tunneld_snapshot(log_path)
        if isinstance(data, dict):
            udids.update(data.keys())
    except Exception:
        pass
    # Also try usbmux list
    try:
        result = subprocess.run(
            [pmd3_bin, "usbmux", "list", "--no-color"],
            capture_output=True, text=True, timeout=CMD_TIMEOUT_SECONDS,
        )
        if result.returncode == 0:
            devices = json.loads(result.stdout)
            if isinstance(devices, list):
                for dev in devices:
                    if isinstance(dev, dict) and dev.get("UniqueDeviceID"):
                        udids.add(dev["UniqueDeviceID"])
    except Exception:
        pass
    return sorted(udids)


def interactive_device_select(udids, devices_data, log_path=None):
    """Prompt user to select a device from a list. Returns UDID."""
    print("[?] 检测到多台设备，请选择：")
    for i, udid in enumerate(udids, 1):
        alias = reverse_alias(udid, devices_data)
        label = f"{alias} ({udid})" if alias else udid
        print(f"  {i}. {label}")
    while True:
        try:
            choice = input("请输入序号: ").strip()
            idx = int(choice) - 1
            if 0 <= idx < len(udids):
                return udids[idx]
        except (ValueError, EOFError, KeyboardInterrupt):
            pass
        print(f"[!] 请输入 1-{len(udids)} 之间的数字。")
```

**Step 2: Rewrite resolve_device_udid**

Replace the existing `resolve_device_udid` function:

```python
def resolve_device_udid(pmd3_bin, log_path=None, device_flag=None):
    """Resolve target device UDID with priority chain:
    1. --device flag (alias or UDID)
    2. SIMLOCATION_UDID env var
    3. devices.json default
    4. Auto-discover (single → auto, multiple → interactive)
    """
    devices_data = read_devices()

    # 1. Explicit --device flag
    if device_flag:
        udid = resolve_alias(device_flag, devices_data)
        log_message(f"[*] 使用指定设备: {udid}", log_path)
        return udid

    # 2. Environment variable
    override = os.environ.get("SIMLOCATION_UDID")
    if override:
        log_message(f"[*] 使用环境变量指定 UDID: {override}", log_path)
        return override

    # 3. Default device from config
    default = devices_data.get("default")
    if default:
        udid = resolve_alias(default, devices_data)
        alias = reverse_alias(udid, devices_data)
        label = f"{alias} ({udid})" if alias else udid
        log_message(f"[*] 使用默认设备: {label}", log_path)
        return udid

    # 4. Auto-discover
    udids = discover_devices(pmd3_bin, log_path)
    if len(udids) == 1:
        log_message(f"[*] 自动发现唯一设备: {udids[0]}", log_path)
        return udids[0]
    if len(udids) > 1:
        return interactive_device_select(udids, devices_data, log_path)

    # Fallback: try lockdown info
    info = run_json_command([pmd3_bin, "lockdown", "info"], log_path)
    if isinstance(info, dict):
        udid = info.get("UniqueDeviceID")
        if udid:
            log_message(f"[*] 通过 lockdown info 获取 UDID: {udid}", log_path)
            return str(udid)

    log_message(
        "[!] 无法自动确定设备 UDID。请连接设备后重试，或设置环境变量 SIMLOCATION_UDID。",
        log_path,
    )
    sys.exit(1)
```

**Step 3: Verify syntax**

Run: `python3 -m py_compile bin/simlocation.py`

**Step 4: Commit**

```bash
git add bin/simlocation.py
git commit -m "feat: multi-device resolve with discovery and interactive selection"
```

---

### Task 3: Refactor callers to use per-device paths

**Files:**
- Modify: `bin/simlocation.py` — update `auto_set_location`, `clear_location`, `start_hold_session`, and `main` block

**Step 1: Update start_hold_session**

Change signature to accept `udid` instead of computing it internally. Replace `pid_path` and `state_path` with per-device paths derived from UDID:

```python
def start_hold_session(
    lat, lon, pmd3_bin, connection_mode, udid, log_path=None
):
    pid_path = pid_path_for(udid)
    state_path = state_path_for(udid)
    stop_hold_session(pid_path, state_path, log_path, quiet=True)

    cmd = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--connection",
        connection_mode,
        "--pid-file",
        str(pid_path),
        "--state-file",
        str(state_path),
        "--_hold-session",
    ]
    if log_path:
        cmd.extend(["--debug", "--log-file", str(log_path)])
    cmd.extend(["set", str(lat), str(lon)])
    child_env = os.environ.copy()
    child_env["SIMLOCATION_UDID"] = udid

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
        env=child_env,
    )

    deadline = time.time() + HOLD_START_TIMEOUT_SECONDS
    while time.time() < deadline:
        state = read_state(state_path)
        if state and state.get("pid") == proc.pid:
            status = state.get("status")
            if status == "ready":
                return True
            if status == "error":
                log_message(
                    f"[!] 后台定位会话启动失败: {state.get('error', 'unknown error')}",
                    log_path,
                )
                return False
        if proc.poll() is not None:
            state = read_state(state_path)
            if state and state.get("status") == "error":
                log_message(
                    f"[!] 后台定位会话启动失败: {state.get('error', 'unknown error')}",
                    log_path,
                )
            else:
                log_message(
                    f"[!] 后台定位进程意外退出，退出码: {proc.returncode}", log_path
                )
            return False
        time.sleep(HOLD_POLL_INTERVAL_SECONDS)

    log_message(
        f"[!] 后台定位会话在 {HOLD_START_TIMEOUT_SECONDS} 秒内未进入 ready 状态。",
        log_path,
    )
    return False
```

**Step 2: Update auto_set_location**

```python
def auto_set_location(
    lat, lon, pmd3_bin, connection_mode="auto", log_path=None, udid=None, device_flag=None,
):
    if not udid:
        udid = resolve_device_udid(pmd3_bin, log_path, device_flag=device_flag)
    message = (
        f"[*] 正在启动后台定位会话，连接模式: {connection_mode}，设备: {udid}。\n"
        f"[*] 后台进程会持续保持 DVT 会话，直到执行 clear。"
    )
    log_message(message, log_path)
    if start_hold_session(lat, lon, pmd3_bin, connection_mode, udid, log_path):
        log_message("[+] 虚拟定位设置成功，后台保持会话已启动。", log_path)
        return
    log_message("[-] 后台定位会话启动失败。", log_path)
    sys.exit(1)
```

**Step 3: Update clear_location**

```python
def clear_location(
    pmd3_bin, connection_mode="auto", log_path=None, udid=None, device_flag=None,
):
    if not udid:
        udid = resolve_device_udid(pmd3_bin, log_path, device_flag=device_flag)
    pid_path = pid_path_for(udid)
    state_path = state_path_for(udid)
    stop_hold_session(pid_path, state_path, log_path, quiet=False)
    # ... rest of function uses udid instead of calling resolve_device_udid again
```

Note: The rest of the `clear_location` function body stays the same, but remove the `resolve_device_udid` call that was inside it and use the `udid` parameter instead.

**Step 4: Update main block**

All callers in `__main__` pass `device_flag=getattr(args, 'device', None)` instead of `pid_path`/`state_path`. The `_hold_session` path still uses the explicit `--pid-file` / `--state-file` args (these are per-device paths passed by the parent).

**Step 5: Verify syntax**

Run: `python3 -m py_compile bin/simlocation.py`

**Step 6: Commit**

```bash
git add bin/simlocation.py
git commit -m "refactor: callers use per-device pid/state paths via UDID"
```

---

### Task 4: Add device subcommands and --device flag to argparse

**Files:**
- Modify: `bin/simlocation.py` — update `parse_args()` (~line 769)

**Step 1: Add --device to main parser and relevant subparsers**

In `parse_args()`, add to the main parser (after `--connection`):

```python
parser.add_argument(
    "--device", "-d",
    default=None,
    help="目标设备（别名或 UDID）",
)
```

Add `device` subparser group:

```python
# simlocation device {list,add,remove,default}
sub_device = subparsers.add_parser("device", help="设备管理")
device_subparsers = sub_device.add_subparsers(dest="device_command")

device_subparsers.add_parser("list", help="列出所有设备")

sub_device_add = device_subparsers.add_parser("add", help="注册设备别名")
sub_device_add.add_argument("alias", help="设备别名")
sub_device_add.add_argument("udid", nargs="?", default=None, help="设备 UDID（省略则交互选择）")

sub_device_remove = device_subparsers.add_parser("remove", help="删除设备别名")
sub_device_remove.add_argument("alias", help="要删除的别名")

sub_device_default = device_subparsers.add_parser("default", help="设置或查看默认设备")
sub_device_default.add_argument("name", nargs="?", default=None, help="别名或 UDID（省略则查看当前默认）")
```

Add `status` and `--all` for `clear`:

```python
# simlocation status
subparsers.add_parser("status", help="查看所有设备定位状态")

# Update clear subparser to add --all
sub_clear = subparsers.add_parser("clear", help="清除虚拟定位，恢复真实位置")
sub_clear.add_argument("--all", action="store_true", dest="clear_all", help="清除所有设备的虚拟定位")
```

Note: the existing `subparsers.add_parser("clear", ...)` line needs to be replaced with `sub_clear = ...` to allow adding `--all`.

**Step 2: Verify syntax**

Run: `python3 -m py_compile bin/simlocation.py`

**Step 3: Commit**

```bash
git add bin/simlocation.py
git commit -m "feat: add device subcommands and --device flag to argparse"
```

---

### Task 5: Implement device subcommand handlers

**Files:**
- Modify: `bin/simlocation.py` — add handler functions and wire into main block

**Step 1: Add device command handlers**

```python
def cmd_device_list(pmd3_bin, log_path=None):
    devices_data = read_devices()
    default = devices_data.get("default")
    default_udid = resolve_alias(default, devices_data) if default else None

    discovered = discover_devices(pmd3_bin, log_path)
    known_udids = set(devices_data["aliases"].values())
    all_udids = sorted(set(discovered) | known_udids)

    if not all_udids:
        print("[*] 未发现任何设备。请检查设备连接和 tunneld 状态。")
        return

    print(f"  {'UDID':<40} {'别名':<12} {'默认':<6} {'状态'}")
    print(f"  {'─' * 40} {'─' * 12} {'─' * 6} {'─' * 20}")
    for udid in all_udids:
        alias = reverse_alias(udid, devices_data) or "—"
        is_default = "✓" if udid == default_udid else "—"
        state = read_state(state_path_for(udid))
        if state and state.get("status") == "ready":
            lat = state.get("lat", "?")
            lon = state.get("lon", "?")
            status_str = f"ready ({lat}, {lon})"
        elif state and state.get("status") == "error":
            status_str = "error"
        else:
            status_str = "—"
        print(f"  {udid:<40} {alias:<12} {is_default:<6} {status_str}")


def cmd_device_add(alias, udid, pmd3_bin, log_path=None):
    devices_data = read_devices()
    if not udid:
        discovered = discover_devices(pmd3_bin, log_path)
        if not discovered:
            print("[!] 未发现任何设备。请连接设备后重试。")
            sys.exit(1)
        if len(discovered) == 1:
            udid = discovered[0]
        else:
            udid = interactive_device_select(discovered, devices_data, log_path)
    devices_data["aliases"][alias] = udid
    write_devices(devices_data)
    print(f"[+] 已注册别名: {alias} → {udid}")


def cmd_device_remove(alias):
    devices_data = read_devices()
    if alias not in devices_data["aliases"]:
        print(f"[!] 别名不存在: {alias}")
        sys.exit(1)
    del devices_data["aliases"][alias]
    if devices_data.get("default") == alias:
        devices_data["default"] = None
        print(f"[*] 默认设备已清除（之前指向已删除的别名 {alias}）。")
    write_devices(devices_data)
    print(f"[+] 已删除别名: {alias}")


def cmd_device_default(name=None):
    devices_data = read_devices()
    if name is None:
        default = devices_data.get("default")
        if default:
            udid = resolve_alias(default, devices_data)
            alias = reverse_alias(udid, devices_data)
            if alias and alias != default:
                print(f"[*] 当前默认设备: {alias} ({udid})")
            elif alias:
                print(f"[*] 当前默认设备: {alias} ({udid})")
            else:
                print(f"[*] 当前默认设备: {udid}")
        else:
            print("[*] 未设置默认设备。")
        return
    # Validate: name must be a known alias or look like a UDID
    if name in devices_data["aliases"] or len(name) > 8:
        devices_data["default"] = name
        write_devices(devices_data)
        udid = resolve_alias(name, devices_data)
        print(f"[+] 默认设备已设置为: {name}" + (f" ({udid})" if name != udid else ""))
    else:
        print(f"[!] 未知的别名或 UDID: {name}")
        sys.exit(1)
```

**Step 2: Add status command handler**

```python
def cmd_status(pmd3_bin, log_path=None):
    cmd_device_list(pmd3_bin, log_path)
```

**Step 3: Add clear --all handler**

```python
def cmd_clear_all(pmd3_bin, connection_mode="auto", log_path=None):
    state_files = sorted(RUNTIME_DIR.glob("*.state.json"))
    active = []
    for sf in state_files:
        state = read_state(sf)
        if state and state.get("status") == "ready":
            udid = sf.stem  # filename is <UDID>.state.json
            active.append(udid)

    if not active:
        print("[*] 没有活跃的定位会话。")
        return

    for udid in active:
        print(f"[*] 正在清除设备 {udid} 的虚拟定位...")
        clear_location(pmd3_bin, connection_mode, log_path, udid=udid)
```

**Step 4: Wire into main block**

In `__main__`, add handling for the new commands:

```python
    elif args.command == "status":
        cmd_status(pmd3_bin, log_path)
    elif args.command == "device":
        dc = getattr(args, "device_command", None)
        if dc == "list":
            cmd_device_list(pmd3_bin, log_path)
        elif dc == "add":
            cmd_device_add(args.alias, getattr(args, "udid", None), pmd3_bin, log_path)
        elif dc == "remove":
            cmd_device_remove(args.alias)
        elif dc == "default":
            cmd_device_default(getattr(args, "name", None))
        else:
            # No device subcommand given, show list
            cmd_device_list(pmd3_bin, log_path)
```

Update existing `clear` handler:

```python
    if args.command == "clear":
        if getattr(args, "clear_all", False):
            cmd_clear_all(pmd3_bin, args.connection, log_path)
        else:
            device_flag = getattr(args, "device", None)
            clear_location(pmd3_bin, args.connection, log_path, device_flag=device_flag)
```

Update `set` and `map` handlers to pass `device_flag`:

```python
    elif args.command == "set":
        device_flag = getattr(args, "device", None)
        auto_set_location(
            args.lat, args.lon, pmd3_bin, args.connection, log_path,
            device_flag=device_flag,
        )
    elif args.command == "map":
        # ... existing map code, but pass device_flag to auto_set_location
```

**Step 5: Verify syntax**

Run: `python3 -m py_compile bin/simlocation.py`

**Step 6: Smoke test**

Run: `python3 bin/simlocation.py device list`
Run: `python3 bin/simlocation.py status`
Run: `python3 bin/simlocation.py device default`

**Step 7: Commit**

```bash
git add bin/simlocation.py
git commit -m "feat: implement device list/add/remove/default and status commands"
```

---

### Task 6: Create initial devices.json with current device as default

**Files:**
- Create: `var/devices.json`

**Step 1: Write initial config**

```json
{
  "default": "00008130-000845CC01EA001C",
  "aliases": {}
}
```

**Step 2: Commit**

```bash
git add var/devices.json
git commit -m "feat: set current device as default in devices.json"
```

---

### Task 7: Migrate away from old single-device state/pid files

**Files:**
- Modify: `bin/simlocation.py` — remove `DEFAULT_PID_PATH`, `DEFAULT_STATE_PATH` from function defaults

**Step 1: Clean up old defaults**

Remove `DEFAULT_PID_PATH` and `DEFAULT_STATE_PATH` constants (keep them only for the `--pid-file`/`--state-file` argparse defaults used by `_hold-session` child processes). The `_hold-session` child process still receives explicit `--pid-file` and `--state-file` from the parent — these are now per-device paths.

Ensure no caller passes the old `simlocation.pid` / `simlocation.state.json` paths. All callers now go through `pid_path_for(udid)` / `state_path_for(udid)`.

**Step 2: Add .gitignore entry**

Add to `var/.gitignore` (or the project root `.gitignore`):

```
var/*.pid
var/*.state.json
```

Keep `var/devices.json` tracked.

**Step 3: Verify syntax**

Run: `python3 -m py_compile bin/simlocation.py`

**Step 4: Commit**

```bash
git add bin/simlocation.py .gitignore
git commit -m "refactor: remove legacy single-device pid/state path defaults"
```

---

### Task 8: Update README

**Files:**
- Modify: `README.md`

**Step 1: Add "连接设备" section after "设备准备"**

Content covers:
- Starting tunneld: `sudo pymobiledevice3 remote tunneld`
- First-time trust: device will prompt "Trust This Computer?"
- Verifying connection: `pymobiledevice3 usbmux list`
- Troubleshooting: restart tunneld, re-plug cable

**Step 2: Add "多设备管理" section after "坐标用法"**

Content covers:
- `simlocation device list` — discover and list devices
- `simlocation device add <alias> [UDID]` — register alias
- `simlocation device default <alias|UDID>` — set default
- `simlocation status` — view all sessions
- `simlocation set --device <alias> <lat> <lon>` — target specific device
- `simlocation clear --device <alias>` / `simlocation clear --all`
- Workflow example from zero

**Step 3: Update "基本准备" section**

Replace "tunneld 正常运行，设备能建立可用连接" with a reference to the new "连接设备" section.

**Step 4: Update CLAUDE.md**

Add `devices.json` to the architecture description. Add new env var notes.

**Step 5: Commit**

```bash
git add README.md CLAUDE.md
git commit -m "docs: add connection guide and multi-device management to README"
```

---

### Task 9: End-to-end verification

**Step 1: Syntax checks**

```bash
python3 -m py_compile bin/simlocation.py
bash -n bin/simlocation
```

**Step 2: CLI smoke tests**

```bash
python3 bin/simlocation.py --help
python3 bin/simlocation.py device --help
python3 bin/simlocation.py device list
python3 bin/simlocation.py device default
python3 bin/simlocation.py status
```

**Step 3: Functional test with real device**

```bash
simlocation set 34.20412 117.13601          # Uses default device
simlocation status                           # Shows ready state
simlocation clear                            # Clears default device
```

**Step 4: Commit any fixes**
