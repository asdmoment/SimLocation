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
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from typing import NoReturn, Optional
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
HOLD_START_TIMEOUT_SECONDS = 12
HOLD_POLL_INTERVAL_SECONDS = 0.25


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


def resolve_device_udid(pmd3_bin, log_path=None):
    override = os.environ.get("SIMLOCATION_UDID")
    if override:
        log_message(f"[*] 使用环境变量指定 UDID: {override}", log_path)
        return override

    info = run_json_command([pmd3_bin, "lockdown", "info"], log_path)
    if isinstance(info, dict):
        udid = info.get("UniqueDeviceID")
        if udid:
            log_message(f"[*] 通过 lockdown info 获取 UDID: {udid}", log_path)
            return str(udid)

    try:
        data = get_tunneld_snapshot(log_path)
        if isinstance(data, dict) and len(data) == 1:
            udid = next(iter(data))
            log_message(f"[*] 通过 tunneld 获取唯一设备 UDID: {udid}", log_path)
            return str(udid)
    except Exception:
        pass

    log_message(
        "[!] 无法自动确定设备 UDID。请连接设备后重试，或设置环境变量 SIMLOCATION_UDID。",
        log_path,
    )
    sys.exit(1)


def request_fresh_rsd(udid, log_path=None):
    last_error = None
    for attempt in range(1, RSD_FETCH_RETRIES + 1):
        try:
            requests.get(
                f"{TUNNELD_URL}/cancel",
                params={"udid": udid},
                timeout=TUNNELD_REQUEST_TIMEOUT_SECONDS,
            )
        except Exception:
            pass

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
            simulation = LocationSimulation(dvt)
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
            simulation = LocationSimulation(dvt)
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
        if connection_mode == "auto":
            rsd_pair = request_fresh_rsd(udid, log_path)
        else:
            rsd_pair = get_latest_rsd(log_path, udid)
        if not rsd_pair:
            raise RuntimeError("未找到有效的 RSD 隧道")
        if not is_rsd_reachable(rsd_pair[0], rsd_pair[1], log_path):
            raise RuntimeError(f"RSD 端口不可达: {rsd_pair[0]}:{rsd_pair[1]}")

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
    lat, lon, pmd3_bin, connection_mode, pid_path, state_path, log_path=None
):
    stop_hold_session(pid_path, state_path, log_path, quiet=True)
    udid = resolve_device_udid(pmd3_bin, log_path)

    cmd = [
        sys.executable,
        str(Path(__file__).resolve()),
        str(lat),
        str(lon),
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


def auto_set_location(
    lat,
    lon,
    pmd3_bin,
    connection_mode="auto",
    log_path=None,
    pid_path=DEFAULT_PID_PATH,
    state_path=DEFAULT_STATE_PATH,
):
    message = (
        f"[*] 正在启动后台定位会话，连接模式: {connection_mode}。\n"
        f"[*] 后台进程会持续保持 DVT 会话，直到执行 clear。"
    )
    log_message(message, log_path)
    if start_hold_session(
        lat, lon, pmd3_bin, connection_mode, pid_path, state_path, log_path
    ):
        log_message("[+] 虚拟定位设置成功，后台保持会话已启动。", log_path)
        return
    log_message("[-] 后台定位会话启动失败。", log_path)
    sys.exit(1)


def clear_location(
    pmd3_bin,
    connection_mode="auto",
    log_path=None,
    pid_path=DEFAULT_PID_PATH,
    state_path=DEFAULT_STATE_PATH,
):
    stop_hold_session(pid_path, state_path, log_path, quiet=False)
    udid = resolve_device_udid(pmd3_bin, log_path)
    for attempt in range(1, COMMAND_RETRIES + 1):
        if connection_mode == "auto":
            rsd_pair = request_fresh_rsd(udid, log_path)
        else:
            rsd_pair = get_latest_rsd(log_path, udid)
        if not rsd_pair:
            log_message("未找到有效的 RSD 隧道，无法清除定位。", log_path)
            sys.exit(1)

        rsd_address, rsd_port = rsd_pair
        if not is_rsd_reachable(rsd_address, rsd_port, log_path):
            if attempt < COMMAND_RETRIES:
                log_message(
                    f"[!] 当前 RSD 不可达，{RETRY_DELAY_SECONDS} 秒后重新获取。",
                    log_path,
                )
                time.sleep(RETRY_DELAY_SECONDS)
                continue
            log_message("[-] 多次获取到的 RSD 都不可达，退出。", log_path)
            sys.exit(1)

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


def exit_coordinate_resolution_error(message) -> NoReturn:
    print(message, file=sys.stderr)
    sys.exit(1)


def resolve_target_coordinates(args) -> tuple[str, str]:
    if args.lat is not None and args.lon is not None:
        return args.lat, args.lon

    env_lat = os.environ.get("SIMLOCATION_DEFAULT_LAT")
    env_lon = os.environ.get("SIMLOCATION_DEFAULT_LON")
    if env_lat is not None and env_lon is not None:
        assert env_lat is not None and env_lon is not None
        return env_lat, env_lon

    if args.lat is not None or args.lon is not None:
        exit_coordinate_resolution_error(
            "CLI 坐标不完整，且环境变量 SIMLOCATION_DEFAULT_LAT 和 SIMLOCATION_DEFAULT_LON 也未同时设置。"
        )

    if env_lat is not None or env_lon is not None:
        exit_coordinate_resolution_error(
            "环境变量 SIMLOCATION_DEFAULT_LAT 和 SIMLOCATION_DEFAULT_LON 必须同时设置。"
        )

    exit_coordinate_resolution_error(
        "未提供坐标。请传入 lat lon，或设置环境变量 SIMLOCATION_DEFAULT_LAT 和 SIMLOCATION_DEFAULT_LON。"
    )


def parse_args():
    parser = argparse.ArgumentParser(
        prog="simlocation",
        description="通过 pymobiledevice3 自动设置 iPhone 虚拟定位。",
    )
    parser.add_argument("lat", nargs="?", help="纬度")
    parser.add_argument("lon", nargs="?", help="经度")
    parser.add_argument(
        "--clear",
        action="store_true",
        help="清除虚拟定位，恢复真实位置",
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
    parser.add_argument("--_hold-session", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "--pid-file", default=str(DEFAULT_PID_PATH), help=argparse.SUPPRESS
    )
    parser.add_argument(
        "--state-file", default=str(DEFAULT_STATE_PATH), help=argparse.SUPPRESS
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    target_lat: Optional[str] = None
    target_lon: Optional[str] = None
    if not args.clear:
        target_lat, target_lon = resolve_target_coordinates(args)

    pmd3_bin = resolve_pymobiledevice3()
    log_path = Path(args.log_file) if args.debug else None
    pid_path = Path(args.pid_file)
    state_path = Path(args.state_file)
    if log_path:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_message("[*] 调试日志已开启。", log_path)
        log_message(f"[*] Python: {sys.executable}", log_path)
        log_message(f"[*] pymobiledevice3: {pmd3_bin}", log_path)
        log_message(f"[*] connection mode: {args.connection}", log_path)
        log_message(f"[*] tunneld URL: {TUNNELD_URL}", log_path)
    if args._hold_session:
        if target_lat is None or target_lon is None:
            raise ValueError("hold session requires lat and lon")
        run_hold_session(
            target_lat,
            target_lon,
            pmd3_bin,
            args.connection,
            pid_path,
            state_path,
            log_path,
        )
        sys.exit(0)
    if args.clear:
        clear_location(pmd3_bin, args.connection, log_path, pid_path, state_path)
    else:
        if target_lat is None or target_lon is None:
            raise ValueError("set location requires lat and lon")
        auto_set_location(
            target_lat,
            target_lon,
            pmd3_bin,
            args.connection,
            log_path,
            pid_path,
            state_path,
        )
