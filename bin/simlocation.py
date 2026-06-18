#!/usr/bin/env python3

import requests
import subprocess
import sys
import shlex
import argparse
import asyncio
import json
import os
import signal
import shutil
import socket
import time
import threading
import webbrowser
from contextlib import suppress
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Optional
from pymobiledevice3.remote.remote_service_discovery import (
    RemoteServiceDiscoveryService,
)
from pymobiledevice3.services.dvt.instruments.location_simulation import (
    LocationSimulation,
)

try:
    # pymobiledevice3 >= 9.x: DvtSecureSocketProxyService 被重构为 DvtProvider
    from pymobiledevice3.services.dvt.instruments.dvt_provider import (
        DvtProvider as DvtSecureSocketProxyService,
    )
except ImportError:
    # pymobiledevice3 < 9.x
    from pymobiledevice3.services.dvt.dvt_secure_socket_proxy import (
        DvtSecureSocketProxyService,
    )

SCRIPT_PATH = Path(__file__).resolve()
PROJECT_DIR = (
    SCRIPT_PATH.parent.parent
    if SCRIPT_PATH.parent.name == "bin"
    else SCRIPT_PATH.parent
)
RUNTIME_DIR = Path(
    os.environ.get("SIMLOCATION_VAR_DIR", str(PROJECT_DIR / "var"))
).expanduser()

# 填入你 tunneld 实际运行的 URL
# 注意：如果 tunneld 重启后端口会变，你需要固定它的端口，或者在脚本里动态寻找
TUNNELD_URL = "http://127.0.0.1:49151"
TUNNELD_REQUEST_TIMEOUT_SECONDS = 5
CMD_TIMEOUT_SECONDS = 8
RSD_FETCH_RETRIES = 3
COMMAND_RETRIES = 3
RETRY_DELAY_SECONDS = 1.5
RSD_CONNECT_TIMEOUT_SECONDS = 2
DEFAULT_LOG_PATH = RUNTIME_DIR / "simlocation.log"
DEFAULT_PID_PATH = RUNTIME_DIR / "simlocation.pid"
DEFAULT_STATE_PATH = RUNTIME_DIR / "simlocation.state.json"
HOLD_START_TIMEOUT_SECONDS = 60
HOLD_POLL_INTERVAL_SECONDS = 0.25

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


def pid_path_for(udid):
    return RUNTIME_DIR / f"{udid}.pid"


def state_path_for(udid):
    return RUNTIME_DIR / f"{udid}.state.json"


def log_message(message, log_path=None):
    print(message)
    if not log_path:
        return
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(log_path, "a", encoding="utf-8") as handle:
        handle.write(f"[{timestamp}] {message}\n")


def write_state(state_path, payload):
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def read_state(state_path):
    if not state_path.exists():
        return None
    try:
        return json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def is_process_alive(pid):
    if sys.platform == "win32":
        import ctypes
        kernel32 = ctypes.windll.kernel32
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if handle:
            kernel32.CloseHandle(handle)
            return True
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def read_pid(pid_path):
    if not pid_path.exists():
        return None
    try:
        return int(pid_path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def remove_file_if_exists(path):
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def get_hold_start_timeout_seconds():
    raw_value = os.environ.get("SIMLOCATION_START_TIMEOUT_SECONDS")
    if raw_value is None:
        return float(HOLD_START_TIMEOUT_SECONDS)
    try:
        timeout_seconds = float(raw_value)
    except ValueError as exc:
        raise ValueError(
            "SIMLOCATION_START_TIMEOUT_SECONDS 必须是大于 0 的数字。"
        ) from exc
    if timeout_seconds <= 0:
        raise ValueError("SIMLOCATION_START_TIMEOUT_SECONDS 必须是大于 0 的数字。")
    return timeout_seconds


def terminate_child_process(proc):
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=CMD_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=CMD_TIMEOUT_SECONDS)


def wait_for_hold_session(
    proc, state_path, timeout_seconds, log_path=None
):
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
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
                    f"[!] 后台定位进程意外退出，退出码: {proc.returncode}",
                    log_path,
                )
            return False
        time.sleep(HOLD_POLL_INTERVAL_SECONDS)

    message = f"后台定位会话在 {timeout_seconds:g} 秒内未进入 ready 状态。"
    log_message(f"[!] {message}", log_path)
    terminate_child_process(proc)
    state = read_state(state_path) or {}
    state.update(
        {
            "status": "error",
            "pid": proc.pid,
            "error": message,
            "failed_at": datetime.now().isoformat(timespec="seconds"),
        }
    )
    write_state(state_path, state)
    return False


def stop_hold_session(pid_path, state_path, log_path=None, quiet=False):
    pid = read_pid(pid_path)
    if pid is None:
        remove_file_if_exists(pid_path)
        return False

    if not is_process_alive(pid):
        if not quiet:
            log_message(
                f"[*] 发现旧的后台定位进程已不存在，清理 PID 文件: {pid}", log_path
            )
        remove_file_if_exists(pid_path)
        return False

    if not quiet:
        log_message(f"[*] 正在停止后台定位会话，PID: {pid}", log_path)

    if sys.platform == "win32":
        import ctypes
        kernel32 = ctypes.windll.kernel32
        PROCESS_TERMINATE = 0x0001
        handle = kernel32.OpenProcess(PROCESS_TERMINATE, False, pid)
        if handle:
            kernel32.TerminateProcess(handle, 1)
            kernel32.CloseHandle(handle)
    else:
        os.kill(pid, signal.SIGTERM)

    deadline = time.time() + CMD_TIMEOUT_SECONDS
    while time.time() < deadline:
        if not is_process_alive(pid):
            break
        time.sleep(HOLD_POLL_INTERVAL_SECONDS)

    if is_process_alive(pid):
        if not quiet:
            log_message(
                f"[!] 后台定位进程未能在 {CMD_TIMEOUT_SECONDS} 秒内退出: {pid}",
                log_path,
            )
        return False

    remove_file_if_exists(pid_path)
    state = read_state(state_path)
    if state:
        state["status"] = "stopped"
        state["stopped_at"] = datetime.now().isoformat(timespec="seconds")
        write_state(state_path, state)
    return True


def resolve_pymobiledevice3():
    override = os.environ.get("SIMLOCATION_PMD3")
    if override:
        if Path(override).is_file() and os.access(override, os.X_OK):
            return override
        print(f"环境变量 SIMLOCATION_PMD3 指向的文件不可执行: {override}")
        sys.exit(1)

    found = shutil.which("pymobiledevice3")
    if found:
        return found

    sibling = str(Path(sys.executable).with_name("pymobiledevice3"))
    if Path(sibling).is_file() and os.access(sibling, os.X_OK):
        return sibling

    print("未找到 pymobiledevice3 可执行文件。")
    print(
        "请安装 pymobiledevice3 后重试，或设置环境变量 SIMLOCATION_PMD3 指向其完整路径。"
    )
    sys.exit(1)


def extract_rsd_pair(data):
    def pick_addr_port(record):
        if not isinstance(record, dict):
            return None
        rsd_address = (
            record.get("rsd_address")
            or record.get("tunnel-address")
            or record.get("tunnel_address")
        )
        rsd_port = (
            record.get("rsd_port")
            or record.get("tunnel-port")
            or record.get("tunnel_port")
        )
        if rsd_address and rsd_port:
            return str(rsd_address), str(rsd_port)
        return None

    if isinstance(data, dict):
        direct = pick_addr_port(data)
        if direct:
            return direct

        for _, info in data.items():
            if isinstance(info, list):
                for item in info:
                    matched = pick_addr_port(item)
                    if matched:
                        return matched
            else:
                matched = pick_addr_port(info)
                if matched:
                    return matched

    if isinstance(data, list):
        for item in data:
            matched = pick_addr_port(item)
            if matched:
                return matched

    return None


def get_tunneld_snapshot(log_path=None):
    response = requests.get(TUNNELD_URL, timeout=TUNNELD_REQUEST_TIMEOUT_SECONDS)
    response.raise_for_status()
    return response.json()


def get_latest_rsd(log_path=None, udid=None):
    last_error = None
    for attempt in range(1, RSD_FETCH_RETRIES + 1):
        try:
            data = get_tunneld_snapshot(log_path)
            if udid and isinstance(data, dict) and udid in data:
                rsd_pair = extract_rsd_pair(data[udid])
            else:
                rsd_pair = extract_rsd_pair(data)
            if rsd_pair:
                log_message(f"[*] 获取到 RSD: {rsd_pair[0]} {rsd_pair[1]}", log_path)
                return rsd_pair
            last_error = f"未在 tunneld 返回中找到可用地址/端口，返回内容: {data}"
        except Exception as e:
            last_error = f"无法获取 RSD 信息，请检查 tunneld 是否正在运行: {e}"

        if attempt < RSD_FETCH_RETRIES:
            log_message(
                f"[!] 获取 RSD 失败，{RETRY_DELAY_SECONDS} 秒后重试 ({attempt}/{RSD_FETCH_RETRIES})。",
                log_path,
            )
            time.sleep(RETRY_DELAY_SECONDS)

    if last_error:
        log_message(last_error, log_path)
    return None


def run_json_command(cmd, log_path=None, timeout=CMD_TIMEOUT_SECONDS):
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        if result.stderr:
            log_message(f"[!] 命令执行失败:\n{result.stderr}", log_path)
        if result.stdout:
            log_message(f"[!] 命令标准输出:\n{result.stdout}", log_path)
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        log_message(f"[!] 无法解析 JSON 输出: {exc}", log_path)
        if result.stdout:
            log_message(f"[!] 原始输出:\n{result.stdout}", log_path)
        return None


def discover_devices(pmd3_bin, log_path=None):
    """Return a list of UDIDs from tunneld and/or lockdown."""
    udids = set()
    try:
        data = get_tunneld_snapshot(log_path)
        if isinstance(data, dict):
            udids.update(data.keys())
    except Exception:
        pass
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


def resolve_device_udid(pmd3_bin, log_path=None, device_flag=None):
    """Resolve target device UDID with priority chain:
    1. --device flag (alias or UDID)
    2. SIMLOCATION_UDID env var
    3. devices.json default
    4. Auto-discover (single -> auto, multiple -> interactive)
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


def request_fresh_rsd(udid, log_path=None):
    last_error = None
    for attempt in range(1, RSD_FETCH_RETRIES + 1):
        for connection_type in ("usbmux", "usb", "wifi", None):
            params = {"udid": udid}
            if connection_type:
                params["connection_type"] = connection_type
            try:
                response = requests.get(
                    f"{TUNNELD_URL}/start-tunnel",
                    params=params,
                    timeout=TUNNELD_REQUEST_TIMEOUT_SECONDS,
                )
                response.raise_for_status()
                data = response.json()
                address = data.get("address")
                port = data.get("port")
                if address and port:
                    rsd_pair = (str(address), str(port))
                    mode_label = connection_type or "auto"
                    log_message(
                        f"[*] 为设备 {udid} 创建新 tunnel 成功 ({mode_label}): {address} {port}",
                        log_path,
                    )
                    return rsd_pair
                last_error = f"tunneld /start-tunnel 返回异常: {data}"
            except Exception as exc:
                mode_label = connection_type or "auto"
                last_error = f"请求新 tunnel 失败 ({mode_label}): {exc}"

        if attempt < RSD_FETCH_RETRIES:
            log_message(
                f"[!] 创建新 tunnel 失败，{RETRY_DELAY_SECONDS} 秒后重试 ({attempt}/{RSD_FETCH_RETRIES})。",
                log_path,
            )
            time.sleep(RETRY_DELAY_SECONDS)

    if last_error:
        log_message(last_error, log_path)
    return None


def acquire_rsd(udid, connection_mode="auto", log_path=None):
    rsd_pair = get_latest_rsd(log_path, udid)
    if rsd_pair and is_rsd_reachable(rsd_pair[0], rsd_pair[1], log_path):
        log_message(
            f"[*] 复用 tunneld 中已有的 RSD: {rsd_pair[0]} {rsd_pair[1]}",
            log_path,
        )
        return rsd_pair

    if connection_mode == "rsd":
        return None

    rsd_pair = request_fresh_rsd(udid, log_path)
    if rsd_pair and is_rsd_reachable(rsd_pair[0], rsd_pair[1], log_path):
        return rsd_pair
    return None


def is_rsd_reachable(host, port, log_path=None):
    try:
        addrinfos = socket.getaddrinfo(host, int(port), type=socket.SOCK_STREAM)
    except Exception as exc:
        log_message(f"[!] 解析 RSD 地址失败: {host}:{port} ({exc})", log_path)
        return False

    last_error = None
    for family, socktype, proto, _, sockaddr in addrinfos:
        sock = None
        try:
            sock = socket.socket(family, socktype, proto)
            sock.settimeout(RSD_CONNECT_TIMEOUT_SECONDS)
            sock.connect(sockaddr)
            log_message(f"[*] RSD 端口可达: {host}:{port}", log_path)
            return True
        except Exception as exc:
            last_error = exc
        finally:
            if sock is not None:
                sock.close()

    log_message(f"[!] RSD 端口不可达: {host}:{port} ({last_error})", log_path)
    return False


def build_location_command(pmd3_bin, action, lat=None, lon=None, rsd_pair=None):
    cmd = [
        pmd3_bin,
        "developer",
        "dvt",
        "simulate-location",
        action,
    ]
    if not rsd_pair:
        raise ValueError("simulate-location requires an rsd pair")
    rsd_address, rsd_port = rsd_pair
    cmd.extend(["--rsd", rsd_address, rsd_port])
    if action == "set":
        cmd.extend(["--", str(lat), str(lon)])
    return cmd


async def _execute_dvt_location_action(rsd_pair, action, lat=None, lon=None):
    async with RemoteServiceDiscoveryService((rsd_pair[0], int(rsd_pair[1]))) as rsd:
        async with DvtSecureSocketProxyService(rsd) as dvt:
            async with LocationSimulation(dvt) as simulation:
                if action == "set":
                    if lat is None or lon is None:
                        raise ValueError("set action requires both lat and lon")
                    await simulation.set(float(lat), float(lon))
                else:
                    await simulation.clear()


def execute_dvt_location_action(rsd_pair, action, lat=None, lon=None, log_path=None):
    try:
        asyncio.run(_execute_dvt_location_action(rsd_pair, action, lat=lat, lon=lon))
        return True
    except Exception as exc:
        log_message(f"[!] DVT {action} 调用失败: {exc}", log_path)
        return False


async def _hold_dvt_location_session(rsd_pair, lat, lon, state_path, log_path=None):
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def request_stop():
        loop.call_soon_threadsafe(stop_event.set)

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, request_stop)
        except NotImplementedError:
            signal.signal(sig, lambda *_: request_stop())

    async with RemoteServiceDiscoveryService((rsd_pair[0], int(rsd_pair[1]))) as rsd:
        async with DvtSecureSocketProxyService(rsd) as dvt:
            async with LocationSimulation(dvt) as simulation:
                await simulation.set(float(lat), float(lon))
                write_state(
                    state_path,
                    {
                        "status": "ready",
                        "pid": os.getpid(),
                        "rsd_address": rsd_pair[0],
                        "rsd_port": rsd_pair[1],
                        "lat": str(lat),
                        "lon": str(lon),
                        "started_at": datetime.now().isoformat(timespec="seconds"),
                    },
                )
                log_message(
                    "[+] 后台定位会话已建立，将持续保持当前位置直到执行 clear。", log_path
                )
                await stop_event.wait()
                with suppress(Exception):
                    await simulation.clear()


def run_hold_session(
    lat, lon, pmd3_bin, connection_mode, pid_path, state_path, log_path=None
):
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text(str(os.getpid()), encoding="utf-8")
    write_state(
        state_path,
        {
            "status": "starting",
            "pid": os.getpid(),
            "lat": str(lat),
            "lon": str(lon),
            "started_at": datetime.now().isoformat(timespec="seconds"),
        },
    )

    try:
        udid = resolve_device_udid(pmd3_bin, log_path)
        rsd_pair = acquire_rsd(udid, connection_mode, log_path)
        if not rsd_pair:
            raise RuntimeError("未找到有效的 RSD 隧道")

        asyncio.run(
            _hold_dvt_location_session(rsd_pair, lat, lon, state_path, log_path)
        )
        state = read_state(state_path) or {}
        state["status"] = "stopped"
        state["stopped_at"] = datetime.now().isoformat(timespec="seconds")
        write_state(state_path, state)
    except Exception as exc:
        write_state(
            state_path,
            {
                "status": "error",
                "pid": os.getpid(),
                "error": str(exc),
                "failed_at": datetime.now().isoformat(timespec="seconds"),
            },
        )
        log_message(f"[-] 后台定位会话启动失败: {exc}", log_path)
        raise
    finally:
        remove_file_if_exists(pid_path)


def start_hold_session(
    lat, lon, pmd3_bin, connection_mode, udid, log_path=None
):
    try:
        timeout_seconds = get_hold_start_timeout_seconds()
    except ValueError as exc:
        log_message(f"[!] {exc}", log_path)
        return False

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

    popen_kwargs = {
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "env": child_env,
    }
    if sys.platform == "win32":
        popen_kwargs["creationflags"] = (
            subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
        )
    else:
        popen_kwargs["start_new_session"] = True
        popen_kwargs["close_fds"] = True
    proc = subprocess.Popen(cmd, **popen_kwargs)
    return wait_for_hold_session(proc, state_path, timeout_seconds, log_path)


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


def clear_location(
    pmd3_bin, connection_mode="auto", log_path=None, udid=None, device_flag=None,
):
    if not udid:
        udid = resolve_device_udid(pmd3_bin, log_path, device_flag=device_flag)
    pid_path = pid_path_for(udid)
    state_path = state_path_for(udid)
    stop_hold_session(pid_path, state_path, log_path, quiet=False)
    for attempt in range(1, COMMAND_RETRIES + 1):
        rsd_pair = acquire_rsd(udid, connection_mode, log_path)
        if not rsd_pair:
            log_message("未找到有效的 RSD 隧道，无法清除定位。", log_path)
            sys.exit(1)

        rsd_address, rsd_port = rsd_pair
        cmd = build_location_command(
            pmd3_bin,
            "clear",
            rsd_pair=rsd_pair,
        )
        if connection_mode == "auto":
            message = f"[*] 已为设备 {udid} 创建新 tunnel，正在清除虚拟定位 ({attempt}/{COMMAND_RETRIES}):\n{shlex.join(cmd)}"
        else:
            message = f"[*] 正在复用现有 RSD 清除虚拟定位 ({attempt}/{COMMAND_RETRIES}):\n{shlex.join(cmd)}"
        log_message(message, log_path)
        if execute_dvt_location_action(rsd_pair, "clear", log_path=log_path):
            log_message("[+] 已清除虚拟定位。", log_path)
            return

        if attempt < COMMAND_RETRIES:
            log_message(
                f"[!] 本次执行未确认成功，{RETRY_DELAY_SECONDS} 秒后重试。", log_path
            )
            time.sleep(RETRY_DELAY_SECONDS)

    log_message("[-] 多次重试后仍未清除成功。", log_path)
    sys.exit(1)


AMAP_KEY_HINT = """\
[*] 提示：当前使用 OpenStreetMap 地图。如需更精细的中国地图，可配置高德 Key：

  1. 前往 https://console.amap.com/ 注册/登录
  2. 进入「应用管理」→「我的应用」→「创建新应用」
  3. 为应用添加一个 Key，服务平台选择「Web端(JS API)」
  4. 设置环境变量：export SIMLOCATION_AMAP_KEY=你的Key
"""

MAP_AMAP_HTML_PATH = PROJECT_DIR / "web" / "map-amap.html"
MAP_OSM_HTML_PATH = PROJECT_DIR / "web" / "map-osm.html"
MAP_SERVER_TIMEOUT_SECONDS = 300


class _MapRequestHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/":
            self.send_error(404)
            return
        html = self.server.map_html
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(html)))
        self.end_headers()
        self.wfile.write(html)

    def do_POST(self):
        if self.path != "/confirm":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        try:
            data = json.loads(body)
            lat = float(data["lat"])
            lon = float(data["lon"])
        except (json.JSONDecodeError, KeyError, ValueError, TypeError):
            self.send_error(400)
            return
        self.server.picked_coords = (lat, lon)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        resp = b'{"ok":true}'
        self.send_header("Content-Length", str(len(resp)))
        self.end_headers()
        self.wfile.write(resp)
        threading.Thread(target=self.server.shutdown, daemon=True).start()

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def log_message(self, format, *args):
        pass


def _open_app_window(url):
    """Try to open URL in a minimal app-like window (no address bar).
    Falls back to regular browser if not available."""
    if sys.platform == "win32":
        chrome_paths = []
        for env_var in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
            base = os.environ.get(env_var, "")
            if base:
                chrome_paths.extend([
                    os.path.join(base, "Google", "Chrome", "Application", "chrome.exe"),
                    os.path.join(base, "Microsoft", "Edge", "Application", "msedge.exe"),
                    os.path.join(base, "BraveSoftware", "Brave-Browser", "Application", "brave.exe"),
                    os.path.join(base, "Chromium", "Application", "chrome.exe"),
                ])
    elif sys.platform == "darwin":
        chrome_paths = [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
        ]
    else:
        # Linux / other Unix
        chrome_paths = []
        for name in ("google-chrome", "google-chrome-stable", "chromium-browser",
                      "chromium", "microsoft-edge", "brave-browser"):
            found = shutil.which(name)
            if found:
                chrome_paths.append(found)

    for path in chrome_paths:
        if Path(path).is_file():
            popen_kwargs = {
                "stdout": subprocess.DEVNULL,
                "stderr": subprocess.DEVNULL,
            }
            if sys.platform == "win32":
                popen_kwargs["creationflags"] = subprocess.DETACHED_PROCESS
            subprocess.Popen([path, f"--app={url}"], **popen_kwargs)
            return
    webbrowser.open(url)


def run_map_picker(amap_key=None):
    if amap_key:
        html_path = MAP_AMAP_HTML_PATH
        provider = "高德地图"
    else:
        html_path = MAP_OSM_HTML_PATH
        provider = "OpenStreetMap"

    if not html_path.is_file():
        print(f"[!] 地图页面文件不存在: {html_path}")
        sys.exit(1)
    template = html_path.read_text(encoding="utf-8")

    server = HTTPServer(("127.0.0.1", 0), _MapRequestHandler)
    port = server.server_address[1]
    html_text = template.replace("{{PORT}}", str(port))
    if amap_key:
        html_text = html_text.replace("{{AMAP_KEY}}", amap_key)
    server.map_html = html_text.encode("utf-8")
    server.picked_coords = None

    url = f"http://127.0.0.1:{port}/"
    print(f"[*] 地图选点服务已启动 ({provider}): {url}")
    print("[*] 正在打开浏览器，请在地图上选择位置后点击「确认」。")
    _open_app_window(url)

    timer = threading.Timer(MAP_SERVER_TIMEOUT_SECONDS, server.shutdown)
    timer.daemon = True
    timer.start()

    server.serve_forever()
    timer.cancel()
    return server.picked_coords


def parse_args():
    parser = argparse.ArgumentParser(
        prog="simlocation",
        description="通过 pymobiledevice3 自动设置 iPhone 虚拟定位。",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="记录详细日志到文件，便于排查热点场景下的不稳定问题",
    )
    parser.add_argument(
        "--log-file",
        default=str(DEFAULT_LOG_PATH),
        help="调试日志文件路径，默认写到项目目录下的 var/simlocation.log",
    )
    parser.add_argument(
        "--connection",
        choices=("auto", "rsd"),
        default="auto",
        help="连接模式。auto 会让 tunneld 为当前设备创建新 tunnel；rsd 复用 tunneld 当前已有的 RSD。",
    )
    parser.add_argument(
        "--device", "-d",
        default=None,
        help="目标设备（别名或 UDID）",
    )
    parser.add_argument("--_hold-session", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "--pid-file", default=str(DEFAULT_PID_PATH), help=argparse.SUPPRESS
    )
    parser.add_argument(
        "--state-file", default=str(DEFAULT_STATE_PATH), help=argparse.SUPPRESS
    )
    # Backward compat: --clear flag (legacy)
    parser.add_argument(
        "--clear",
        action="store_true",
        help=argparse.SUPPRESS,
    )

    subparsers = parser.add_subparsers(dest="command")

    # simlocation set <lat> <lon>
    sub_set = subparsers.add_parser("set", help="设置虚拟定位")
    sub_set.add_argument("lat", help="纬度")
    sub_set.add_argument("lon", help="经度")

    # simlocation clear
    sub_clear = subparsers.add_parser("clear", help="清除虚拟定位，恢复真实位置")
    sub_clear.add_argument("--all", action="store_true", dest="clear_all", help="清除所有设备的虚拟定位")

    # simlocation map [--pick-only]
    sub_map = subparsers.add_parser("map", help="打开地图选点，选择后自动设置定位")
    sub_map.add_argument(
        "--pick-only",
        action="store_true",
        help="仅选点并输出坐标，不自动设置定位",
    )

    # simlocation status
    subparsers.add_parser("status", help="查看所有设备定位状态")

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

    # Try normal parse first; if it fails on subcommand matching,
    # fall back to legacy positional arg handling.
    try:
        _stderr = sys.stderr
        sys.stderr = open(os.devnull, "w")
        args, remaining = parser.parse_known_args()
        sys.stderr = _stderr
    except SystemExit as e:
        sys.stderr = _stderr
        if e.code == 0:
            # --help or similar triggered a clean exit
            sys.exit(0)
        # argparse exits on error — intercept to handle legacy format.
        # Re-parse without subparsers: strip argv to find bare lat/lon.
        raw = sys.argv[1:]
        legacy_parser = argparse.ArgumentParser(add_help=False)
        legacy_parser.add_argument("--clear", action="store_true")
        legacy_parser.add_argument("--debug", action="store_true")
        legacy_parser.add_argument("--log-file", default=str(DEFAULT_LOG_PATH))
        legacy_parser.add_argument("--connection", choices=("auto", "rsd"), default="auto")
        legacy_parser.add_argument("--device", "-d", default=None)
        legacy_parser.add_argument("--_hold-session", action="store_true")
        legacy_parser.add_argument("--pid-file", default=str(DEFAULT_PID_PATH))
        legacy_parser.add_argument("--state-file", default=str(DEFAULT_STATE_PATH))
        largs, positional = legacy_parser.parse_known_args(raw)

        args = largs
        args.command = None
        remaining = positional

    # Backward compat: simlocation <lat> <lon> (no subcommand)
    if args.command is None and not getattr(args, "clear", False) and not getattr(args, "_hold_session", False):
        if len(remaining) >= 2:
            args.command = "set"
            args.lat = remaining[0]
            args.lon = remaining[1]
        elif len(remaining) == 0:
            env_lat = os.environ.get("SIMLOCATION_DEFAULT_LAT")
            env_lon = os.environ.get("SIMLOCATION_DEFAULT_LON")
            if env_lat is not None and env_lon is not None:
                args.command = "set"
                args.lat = env_lat
                args.lon = env_lon
            else:
                parser.print_help()
                sys.exit(1)
        elif remaining:
            parser.error(f"无法识别的参数: {' '.join(remaining)}")

    # Backward compat: --clear flag
    if getattr(args, "clear", False) and args.command is None:
        args.command = "clear"

    # _hold-session needs lat/lon
    if getattr(args, "_hold_session", False) and args.command is None:
        if len(remaining) >= 2:
            args.command = "set"
            args.lat = remaining[0]
            args.lon = remaining[1]
        else:
            env_lat = os.environ.get("SIMLOCATION_DEFAULT_LAT")
            env_lon = os.environ.get("SIMLOCATION_DEFAULT_LON")
            if env_lat is not None and env_lon is not None:
                args.command = "set"
                args.lat = env_lat
                args.lon = env_lon
            else:
                parser.error("_hold-session 需要坐标参数。")

    return args


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
            if alias:
                print(f"[*] 当前默认设备: {alias} ({udid})")
            else:
                print(f"[*] 当前默认设备: {udid}")
        else:
            print("[*] 未设置默认设备。")
        return
    devices_data["default"] = name
    write_devices(devices_data)
    udid = resolve_alias(name, devices_data)
    print(f"[+] 默认设备已设置为: {name}" + (f" ({udid})" if name != udid else ""))


def cmd_status(pmd3_bin, log_path=None):
    cmd_device_list(pmd3_bin, log_path)


def cmd_clear_all(pmd3_bin, connection_mode="auto", log_path=None):
    state_files = sorted(RUNTIME_DIR.glob("*.state.json"))
    active = []
    for sf in state_files:
        state = read_state(sf)
        if state and state.get("status") == "ready":
            udid = sf.name.removesuffix(".state.json")
            active.append(udid)

    if not active:
        print("[*] 没有活跃的定位会话。")
        return

    for udid in active:
        print(f"[*] 正在清除设备 {udid} 的虚拟定位...")
        clear_location(pmd3_bin, connection_mode, log_path, udid=udid)


if __name__ == "__main__":
    args = parse_args()

    pmd3_bin = resolve_pymobiledevice3()
    log_path = Path(args.log_file) if args.debug else None

    if log_path:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_message("[*] 调试日志已开启。", log_path)
        log_message(f"[*] Python: {sys.executable}", log_path)
        log_message(f"[*] pymobiledevice3: {pmd3_bin}", log_path)
        log_message(f"[*] connection mode: {args.connection}", log_path)
        log_message(f"[*] platform: {sys.platform}", log_path)
        log_message(f"[*] tunneld URL: {TUNNELD_URL}", log_path)

    if args._hold_session:
        pid_path = Path(args.pid_file)
        state_path = Path(args.state_file)
        run_hold_session(
            args.lat,
            args.lon,
            pmd3_bin,
            args.connection,
            pid_path,
            state_path,
            log_path,
        )
        sys.exit(0)

    device_flag = getattr(args, "device", None)

    if args.command == "status":
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
            cmd_device_list(pmd3_bin, log_path)
    elif args.command == "clear":
        if getattr(args, "clear_all", False):
            cmd_clear_all(pmd3_bin, args.connection, log_path)
        else:
            clear_location(pmd3_bin, args.connection, log_path, device_flag=device_flag)
    elif args.command == "map":
        amap_key = os.environ.get("SIMLOCATION_AMAP_KEY", "").strip() or None
        if not amap_key:
            print(AMAP_KEY_HINT)
        coords = run_map_picker(amap_key)
        if coords is None:
            print("[-] 未选择坐标（超时或关闭了浏览器）。")
            sys.exit(1)
        lat, lon = coords
        if getattr(args, "pick_only", False):
            print(f"{lat:.6f} {lon:.6f}")
        else:
            print(f"[+] 已选择坐标: {lat:.6f}, {lon:.6f}")
            auto_set_location(
                str(lat),
                str(lon),
                pmd3_bin,
                args.connection,
                log_path,
                device_flag=device_flag,
            )
    elif args.command == "set":
        auto_set_location(
            args.lat,
            args.lon,
            pmd3_bin,
            args.connection,
            log_path,
            device_flag=device_flag,
        )
