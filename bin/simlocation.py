#!/usr/bin/env python3

import argparse
import asyncio
import json
import math
import os
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
import xml.etree.ElementTree as ET
from bisect import bisect_right
from contextlib import suppress
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Optional

import requests
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
RSD_CONNECT_RETRIES = 3
DEFAULT_LOG_PATH = RUNTIME_DIR / "simlocation.log"
DEFAULT_PID_PATH = RUNTIME_DIR / "simlocation.pid"
DEFAULT_STATE_PATH = RUNTIME_DIR / "simlocation.state.json"
HOLD_START_TIMEOUT_SECONDS = 12
HOLD_POLL_INTERVAL_SECONDS = 0.25
ROUTE_UPDATE_INTERVAL_SECONDS = 1.0
MAX_ROUTE_BYTES = 4 * 1024 * 1024
MAX_ROUTE_POINTS = 10000
EARTH_RADIUS_METERS = 6371008.8

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
    # Readers must never see a partially written route progress update.
    temporary = state_path.with_name(f"{state_path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temporary.replace(state_path)
    finally:
        remove_file_if_exists(temporary)


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


def stop_hold_session(pid_path, state_path, log_path=None, quiet=False):
    pid = read_pid(pid_path)
    if pid is None:
        remove_file_if_exists(pid_path)
        return True

    if not is_process_alive(pid):
        if not quiet:
            log_message(
                f"[*] 发现旧的后台定位进程已不存在，清理 PID 文件: {pid}", log_path
            )
        remove_file_if_exists(pid_path)
        return True

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
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except OSError as exc:
            log_message(f"[!] 无法停止后台定位进程 {pid}: {exc}", log_path)
            return False

    deadline = time.monotonic() + CMD_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
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
            if udid:
                rsd_pair = (
                    extract_rsd_pair(data[udid])
                    if isinstance(data, dict) and udid in data
                    else None
                )
                last_error = f"未在 tunneld 返回中找到设备 {udid} 的可用地址/端口。"
            else:
                rsd_pair = extract_rsd_pair(data)
                last_error = "未在 tunneld 返回中找到可用地址/端口。"
            if rsd_pair:
                log_message(f"[*] 获取到 RSD: {rsd_pair[0]} {rsd_pair[1]}", log_path)
                return rsd_pair
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
            [pmd3_bin, "usbmux", "list"],
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


def wait_for_rsd_reachable(rsd_pair, log_path=None):
    """Allow a newly created tunnel's route to become usable before failing."""
    for attempt in range(1, RSD_CONNECT_RETRIES + 1):
        if is_rsd_reachable(rsd_pair[0], rsd_pair[1], log_path):
            return True
        if attempt < RSD_CONNECT_RETRIES:
            log_message(
                f"[!] 等待 RSD 连接就绪，{RETRY_DELAY_SECONDS} 秒后重试 "
                f"({attempt}/{RSD_CONNECT_RETRIES})。",
                log_path,
            )
            time.sleep(RETRY_DELAY_SECONDS)
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


def validate_coordinates(lat, lon):
    values = []
    for name, value, limit in (("纬度", lat, 90), ("经度", lon, 180)):
        if isinstance(value, bool):
            raise ValueError(f"{name}必须是数字。")
        try:
            value = float(value)
        except (ValueError, TypeError, OverflowError):
            raise ValueError(f"{name}必须是数字。") from None
        if not math.isfinite(value) or not -limit <= value <= limit:
            raise ValueError(f"{name}必须在 {-limit} 到 {limit} 之间。")
        values.append(value)
    return tuple(values)


def route_distance(start, end):
    lat1, lon1, lat2, lon2 = map(math.radians, (*start, *end))
    a = (math.sin((lat2 - lat1) / 2) ** 2
         + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2)
    return 2 * EARTH_RADIUS_METERS * math.asin(math.sqrt(min(1.0, max(0.0, a))))


class Route:
    """A polyline measured in meters, interpolated along great-circle segments."""

    def __init__(self, points, loop=False):
        if not isinstance(points, (list, tuple)) or not 2 <= len(points) <= MAX_ROUTE_POINTS:
            raise ValueError(f"路线需要 2 到 {MAX_ROUTE_POINTS} 个坐标点。")
        self.points = []
        for index, point in enumerate(points, 1):
            if not isinstance(point, (list, tuple)) or len(point) != 2:
                raise ValueError(f"第 {index} 个点应为 [纬度, 经度]。")
            point = validate_coordinates(*point)
            if not self.points or route_distance(self.points[-1], point) > 0.001:
                self.points.append(point)
        if len(self.points) < 2:
            raise ValueError("路线需要至少两个不同的位置。")
        if loop:
            if route_distance(self.points[-1], self.points[0]) > 0.001:
                self.points.append(self.points[0])
            else:
                self.points[-1] = self.points[0]
        self.cumulative = [0.0]
        for start, end in zip(self.points, self.points[1:]):
            distance = route_distance(start, end)
            if distance / EARTH_RADIUS_METERS >= math.pi - 1e-6:
                raise ValueError("路线包含相对的地球两端，请增加中间途经点。")
            self.cumulative.append(self.cumulative[-1] + distance)
        self.total_m = self.cumulative[-1]

    def position(self, distance_m):
        if distance_m <= 0:
            return self.points[0]
        if distance_m >= self.total_m:
            return self.points[-1]
        index = bisect_right(self.cumulative, distance_m) - 1
        segment_m = self.cumulative[index + 1] - self.cumulative[index]
        fraction = (distance_m - self.cumulative[index]) / segment_m
        angle = segment_m / EARTH_RADIUS_METERS
        weights = (math.sin((1 - fraction) * angle) / math.sin(angle),
                   math.sin(fraction * angle) / math.sin(angle))
        vectors = []
        for lat, lon in self.points[index:index + 2]:
            lat, lon = math.radians(lat), math.radians(lon)
            vectors.append((math.cos(lat) * math.cos(lon),
                            math.cos(lat) * math.sin(lon), math.sin(lat)))
        x, y, z = (sum(weight * vector[axis] for weight, vector in zip(weights, vectors))
                   for axis in range(3))
        return math.degrees(math.atan2(z, math.hypot(x, y))), math.degrees(math.atan2(y, x))


def load_route(path, loop=False):
    path = Path(path).expanduser()
    try:
        with path.open("rb") as handle:
            data = handle.read(MAX_ROUTE_BYTES + 1)
        if len(data) > MAX_ROUTE_BYTES:
            raise ValueError("路线文件不能超过 4 MiB。")
        if path.suffix.lower() == ".gpx":
            root = ET.fromstring(data)
            segments = []
            for element in root.iter():
                tag = element.tag.rsplit("}", 1)[-1]
                if tag in ("trkseg", "rte"):
                    point_tag = "trkpt" if tag == "trkseg" else "rtept"
                    points = [(point.get("lat"), point.get("lon")) for point in element
                              if point.tag.rsplit("}", 1)[-1] == point_tag]
                    if points:
                        segments.append(points)
            if len(segments) != 1:
                raise ValueError("GPX 需要包含一条连续的 track segment 或 route。")
            points = segments[0]
        else:
            decoded = json.loads(data)
            points = decoded.get("points") if isinstance(decoded, dict) else decoded
        return Route(points, loop=loop)
    except (OSError, ValueError, ET.ParseError, RecursionError) as exc:
        raise ValueError(f"无法读取路线 {path.name}: {exc}") from exc


async def play_route(simulation, route, speed_kmh, loop_route, stop_event, state, state_path,
                     clock=time.monotonic):
    started = clock()
    while not stop_event.is_set():
        traveled_m = (clock() - started) * speed_kmh / 3.6
        distance_m = traveled_m % route.total_m if loop_route else min(traveled_m, route.total_m)
        lat, lon = route.position(distance_m)
        await simulation.set(lat, lon)
        completed = not loop_route and distance_m >= route.total_m
        state.update(lat=str(lat), lon=str(lon), route_phase="completed" if completed else "moving",
                     distance_m=distance_m, progress=distance_m / route.total_m,
                     lap=int(traveled_m // route.total_m) + 1 if loop_route else 1)
        write_state(state_path, state)
        if completed:
            await stop_event.wait()
            return
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=ROUTE_UPDATE_INTERVAL_SECONDS)
        except asyncio.TimeoutError:
            pass


async def _hold_dvt_location_session(
    rsd_pair, lat, lon, state_path, log_path=None, *, route=None, speed_kmh=5.0, loop_route=False
):
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
                state = {
                    "status": "ready",
                    "pid": os.getpid(),
                    "rsd_address": rsd_pair[0],
                    "rsd_port": rsd_pair[1],
                    "lat": str(lat),
                    "lon": str(lon),
                    "started_at": datetime.now().isoformat(timespec="seconds"),
                }
                if route:
                    state.update(mode="route", route_phase="moving", speed_kmh=speed_kmh,
                                 loop=loop_route, total_m=route.total_m, distance_m=0.0,
                                 progress=0.0, lap=1)
                write_state(state_path, state)
                log_message(
                    "[+] 后台定位会话已建立，将持续保持当前位置直到执行 clear。", log_path
                )
                try:
                    if route:
                        await play_route(simulation, route, speed_kmh, loop_route,
                                         stop_event, state, state_path)
                    else:
                        await stop_event.wait()
                finally:
                    with suppress(Exception):
                        await simulation.clear()


def run_hold_session(
    lat, lon, pmd3_bin, connection_mode, pid_path, state_path, log_path=None,
    *, route=None, speed_kmh=5.0, loop_route=False,
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
        if not wait_for_rsd_reachable(rsd_pair, log_path):
            raise RuntimeError(f"RSD 端口不可达: {rsd_pair[0]}:{rsd_pair[1]}")

        asyncio.run(
            _hold_dvt_location_session(rsd_pair, lat, lon, state_path, log_path,
                                       route=route, speed_kmh=speed_kmh, loop_route=loop_route)
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
        log_message(f"[-] 后台定位会话失败: {exc}", log_path)
        raise
    finally:
        remove_file_if_exists(pid_path)


def cleanup_failed_hold_session(proc, pid_path, state_path, log_path=None):
    """Reap the child before reporting a failed startup, preserving other sessions."""
    try:
        if proc.poll() is None:
            with suppress(ProcessLookupError):
                proc.terminate()
        try:
            proc.wait(timeout=CMD_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            with suppress(ProcessLookupError):
                proc.kill()
            proc.wait(timeout=CMD_TIMEOUT_SECONDS)
    except (OSError, subprocess.TimeoutExpired) as exc:
        log_message(f"[!] 无法回收启动失败的后台进程 {proc.pid}: {exc}", log_path)
        return

    if read_pid(pid_path) == proc.pid:
        remove_file_if_exists(pid_path)
    state = read_state(state_path)
    if state and state.get("pid") == proc.pid and state.get("status") != "error":
        state["status"] = "error"
        state["error"] = "后台定位会话未成功启动，进程已停止。"
        state["failed_at"] = datetime.now().isoformat(timespec="seconds")
        write_state(state_path, state)


def start_hold_session(
    lat, lon, pmd3_bin, connection_mode, udid, log_path=None,
    *, route_file=None, speed_kmh=5.0, loop_route=False,
):
    pid_path = pid_path_for(udid)
    state_path = state_path_for(udid)
    if not stop_hold_session(pid_path, state_path, log_path, quiet=True):
        log_message("[!] 旧后台定位会话未停止，取消启动新会话。", log_path)
        return False

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
    if route_file:
        cmd.extend(["route", str(route_file), "--speed", str(speed_kmh)])
        if loop_route:
            cmd.append("--loop")
    else:
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

    ready = False
    try:
        deadline = time.monotonic() + HOLD_START_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            state = read_state(state_path)
            if state and state.get("pid") == proc.pid:
                status = state.get("status")
                if status == "ready" and proc.poll() is None:
                    ready = True
                    return True
                if status == "error":
                    log_message(
                        f"[!] 后台定位会话启动失败: {state.get('error', 'unknown error')}",
                        log_path,
                    )
                    return False
            if proc.poll() is not None:
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
    finally:
        if not ready:
            cleanup_failed_hold_session(proc, pid_path, state_path, log_path)


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


def auto_set_route(route, speed_kmh, loop_route, pmd3_bin, connection_mode="auto",
                   log_path=None, device_flag=None):
    udid = resolve_device_udid(pmd3_bin, log_path, device_flag=device_flag)
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    # Give the worker an immutable snapshot, independent of later file edits.
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", prefix="route-",
                                     dir=RUNTIME_DIR, encoding="utf-8", delete=False) as handle:
        snapshot = Path(handle.name)
        json.dump({"points": route.points[:-1] if loop_route else route.points}, handle)
    try:
        log_message(f"[*] 正在启动运动轨迹: {route.total_m:.0f} m，{speed_kmh:g} km/h。", log_path)
        if start_hold_session(*route.points[0], pmd3_bin, connection_mode, udid, log_path,
                              route_file=snapshot, speed_kmh=speed_kmh, loop_route=loop_route):
            log_message("[+] 运动轨迹已启动。使用 status 查看进度，clear 结束模拟定位。", log_path)
            return
        log_message("[-] 运动轨迹启动失败。", log_path)
        sys.exit(1)
    finally:
        remove_file_if_exists(snapshot)


def clear_location(
    pmd3_bin, connection_mode="auto", log_path=None, udid=None, device_flag=None,
):
    if not udid:
        udid = resolve_device_udid(pmd3_bin, log_path, device_flag=device_flag)
    pid_path = pid_path_for(udid)
    state_path = state_path_for(udid)
    if not stop_hold_session(pid_path, state_path, log_path, quiet=False):
        log_message("[!] 后台定位会话未停止，取消清除操作。", log_path)
        sys.exit(1)
    for attempt in range(1, COMMAND_RETRIES + 1):
        if connection_mode == "auto":
            rsd_pair = request_fresh_rsd(udid, log_path)
        else:
            rsd_pair = get_latest_rsd(log_path, udid)
        if not rsd_pair:
            log_message("未找到有效的 RSD 隧道，无法清除定位。", log_path)
            sys.exit(1)

        if not wait_for_rsd_reachable(rsd_pair, log_path):
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
ROUTE_MAP_TIMEOUT_SECONDS = 1800


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
        try:
            length = int(self.headers.get("Content-Length", 0))
            if not 0 < length <= MAX_ROUTE_BYTES:
                raise ValueError("提交内容过大或为空。")
            body = self.rfile.read(length)
            data = json.loads(body)
            if self.server.route_mode:
                # Validate before accepting or shutting down the picker.
                points = data["points"]
                Route(points, loop=self.server.loop_route)
                picked = points
            else:
                picked = validate_coordinates(data["lat"], data["lon"])
        except (KeyError, ValueError, TypeError):
            self.send_error(400)
            return
        self.server.picked_coords = picked
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


def run_map_picker(amap_key=None, *, route_mode=False, speed_kmh=5.0,
                   loop_route=False, pick_only=False):
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
    route_script = (PROJECT_DIR / "web" / "map-route.js").read_text(encoding="utf-8")
    html_text = html_text.replace("{{ROUTE_SCRIPT}}", route_script)
    html_text = html_text.replace("{{ROUTE_MODE}}", "true" if route_mode else "false")
    html_text = html_text.replace("{{ROUTE_LOOP}}", "true" if loop_route else "false")
    html_text = html_text.replace("{{PICK_ONLY}}", "true" if pick_only else "false")
    html_text = html_text.replace("{{ROUTE_SPEED}}", str(speed_kmh))
    if amap_key:
        html_text = html_text.replace("{{AMAP_KEY}}", amap_key)
    server.map_html = html_text.encode("utf-8")
    server.picked_coords = None
    server.route_mode = route_mode
    server.loop_route = loop_route

    url = f"http://127.0.0.1:{port}/"
    print(f"[*] 地图选点服务已启动 ({provider}): {url}")
    print("[*] 正在打开浏览器，请按顺序添加途经点后确认路线。" if route_mode
          else "[*] 正在打开浏览器，请在地图上选择位置后点击「确认」。")
    _open_app_window(url)

    timeout_seconds = ROUTE_MAP_TIMEOUT_SECONDS if route_mode else MAP_SERVER_TIMEOUT_SECONDS
    timer = threading.Timer(timeout_seconds, server.shutdown)
    timer.daemon = True
    timer.start()

    try:
        server.serve_forever()
    finally:
        timer.cancel()
        server.server_close()
    return server.picked_coords


def parse_args(argv=None):
    common = argparse.ArgumentParser(prog="simlocation", add_help=False, allow_abbrev=False)
    common.add_argument(
        "--debug",
        action="store_true",
        help="记录详细日志到文件，便于排查热点场景下的不稳定问题",
    )
    common.add_argument(
        "--log-file",
        default=str(DEFAULT_LOG_PATH),
        help="调试日志文件路径，默认写到项目目录下的 var/simlocation.log",
    )
    common.add_argument(
        "--connection",
        choices=("auto", "rsd"),
        default="auto",
        help="连接模式。auto 会让 tunneld 为当前设备创建新 tunnel；rsd 复用 tunneld 当前已有的 RSD。",
    )
    common.add_argument(
        "--device", "-d",
        default=None,
        help="目标设备（别名或 UDID）",
    )
    common.add_argument("--_hold-session", action="store_true", help=argparse.SUPPRESS)
    common.add_argument(
        "--pid-file", default=str(DEFAULT_PID_PATH), help=argparse.SUPPRESS
    )
    common.add_argument(
        "--state-file", default=str(DEFAULT_STATE_PATH), help=argparse.SUPPRESS
    )
    # Backward compat: --clear flag (legacy)
    common.add_argument(
        "--clear",
        action="store_true",
        help=argparse.SUPPRESS,
    )

    parser = argparse.ArgumentParser(
        prog="simlocation",
        description="通过 pymobiledevice3 自动设置 iPhone 虚拟定位。",
        parents=[common],
        allow_abbrev=False,
    )
    subparsers = parser.add_subparsers(dest="command")

    # simlocation set <lat> <lon>
    sub_set = subparsers.add_parser("set", help="设置虚拟定位", allow_abbrev=False)
    sub_set.add_argument("lat", help="纬度")
    sub_set.add_argument("lon", help="经度")

    sub_route = subparsers.add_parser("route", help="按路线模拟移动", allow_abbrev=False)
    sub_route.add_argument("file", nargs="?", help="JSON 或 GPX 路线文件；省略时打开地图绘制路线")
    sub_route.add_argument("--speed", type=float, default=5.0, help="移动速度，单位 km/h，默认 5")
    sub_route.add_argument("--loop", action="store_true", help="从终点连回起点，循环移动")
    sub_route.add_argument("--pick-only", action="store_true", help="只在地图上编辑和保存路线，不设置定位")

    # simlocation clear
    sub_clear = subparsers.add_parser("clear", help="清除虚拟定位，恢复真实位置", allow_abbrev=False)
    sub_clear.add_argument("--all", action="store_true", dest="clear_all", help="清除所有设备的虚拟定位")

    # simlocation map [--pick-only]
    sub_map = subparsers.add_parser("map", help="打开地图选点，选择后自动设置定位", allow_abbrev=False)
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

    # Extract shared options first so they work on either side of a subcommand.
    # The final strict parse rejects every unconsumed option or positional arg.
    shared_args, remaining = common.parse_known_args(argv)
    if shared_args.clear:
        if remaining and remaining[0] != "clear":
            parser.error("--clear 不能与其他命令或坐标一起使用。")
        if not remaining:
            remaining = ["clear"]
    elif not remaining:
        env_lat = os.environ.get("SIMLOCATION_DEFAULT_LAT")
        env_lon = os.environ.get("SIMLOCATION_DEFAULT_LON")
        if env_lat is not None and env_lon is not None:
            remaining = ["set", "--", env_lat, env_lon]
        else:
            parser.print_help()
            sys.exit(1)
    elif remaining[0] == "--":
        remaining.insert(0, "set")
    else:
        # Only a numeric first argument can select legacy coordinate syntax.
        try:
            float(remaining[0])
        except ValueError:
            pass
        else:
            remaining.insert(0, "set")

    # argparse otherwise treats negative scientific notation as an option.
    if remaining[0] == "set" and len(remaining) == 3:
        try:
            float(remaining[1])
            float(remaining[2])
        except ValueError:
            pass
        else:
            remaining.insert(1, "--")

    args = parser.parse_args(remaining, namespace=shared_args)
    if args._hold_session and args.command not in ("set", "route"):
        parser.error("_hold-session 只能用于 set 或 route 命令。")
    if args.command == "route":
        if not math.isfinite(args.speed) or not 0 < args.speed <= 1000:
            parser.error("--speed 必须大于 0 且不超过 1000 km/h。")
        if args.pick_only and (args.file or args._hold_session):
            parser.error("--pick-only 仅用于地图路线编辑。")
        if args._hold_session and not args.file:
            parser.error("后台运动轨迹需要路线文件。")
        try:
            args.route = load_route(args.file, loop=args.loop) if args.file else None
        except ValueError as exc:
            parser.error(str(exc))
    if args.command == "set":
        for name, limit in (("lat", 90), ("lon", 180)):
            value = getattr(args, name)
            try:
                number = float(value)
            except ValueError:
                parser.error(f"{name} 必须是有效数字: {value}")
            if not math.isfinite(number) or not -limit <= number <= limit:
                parser.error(f"{name} 必须在 {-limit} 到 {limit} 之间: {value}")

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
            if state.get("mode") == "route":
                if state.get("route_phase") == "completed":
                    status_str = f"已到终点，保持定位 ({lat}, {lon})"
                else:
                    status_str = (f"移动中 {state.get('progress', 0):.0%} "
                                  f"{state.get('speed_kmh', 0):g} km/h ({lat}, {lon})")
                    if state.get("loop"):
                        status_str += f" 第 {state.get('lap', 1)} 圈"
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
        route = getattr(args, "route", None)
        lat, lon = route.points[0] if route else (args.lat, args.lon)
        run_hold_session(
            lat,
            lon,
            pmd3_bin,
            args.connection,
            pid_path,
            state_path,
            log_path,
            route=route,
            speed_kmh=getattr(args, "speed", 5.0),
            loop_route=getattr(args, "loop", False),
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
    elif args.command == "route":
        route = args.route
        if route is None:
            amap_key = os.environ.get("SIMLOCATION_AMAP_KEY", "").strip() or None
            points = run_map_picker(amap_key, route_mode=True, speed_kmh=args.speed,
                                    loop_route=args.loop, pick_only=args.pick_only)
            if points is None:
                print("[-] 未提交路线。")
                sys.exit(1)
            route = Route(points, loop=args.loop)
        if args.pick_only:
            print(json.dumps({"points": route.points[:-1] if args.loop else route.points}))
        else:
            auto_set_route(route, args.speed, args.loop, pmd3_bin, args.connection,
                           log_path, device_flag=device_flag)
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
