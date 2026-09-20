#!/usr/bin/env python3

import requests
import subprocess
import sys
import argparse
import asyncio
import ipaddress
import json
import math
import os
import re
import secrets
import signal
import shutil
import socket
import tempfile
import time
import threading
import unicodedata
import webbrowser
import xml.etree.ElementTree as ET
from bisect import bisect_right
from contextlib import redirect_stderr, suppress
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlsplit, parse_qs, quote
from importlib import metadata as importlib_metadata
from pathlib import Path
from pymobiledevice3.remote.remote_service_discovery import (
    RemoteServiceDiscoveryService,
)
from pymobiledevice3.services.dvt.instruments.location_simulation import (
    LocationSimulation,
)

try:
    # defusedxml 是 pymobiledevice3 的硬依赖，能跑本 CLI 的环境里必然有它。
    # 标准库 ElementTree 不解析外部实体（XXE 打不通），但会展开内部实体，
    # 1 KB 的 GPX 可以膨胀成几百 MB。
    from defusedxml.ElementTree import fromstring as xml_fromstring
except ImportError:  # pragma: no cover - 只有装坏了的环境会走到这里
    xml_fromstring = None

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
TUNNELD_URL = os.environ.get("SIMLOCATION_TUNNELD_URL", "http://127.0.0.1:49151").rstrip("/")
TUNNELD_REQUEST_TIMEOUT_SECONDS = 5
# tunneld's /start-tunnel blocks until the tunnel is up (or fails); over Wi-Fi /
# hotspot that regularly takes tens of seconds. Keep this below the default
# HOLD_START_TIMEOUT_SECONDS so the foreground waiter outlives the request.
# tunneld tries every transport itself, but a plain /start-tunnel request on a
# device whose tunnel is dead burns its whole timeout inside a bonjour scan.
# Explicit connection_type requests bypass that: usbmux either answers in
# fractions of a second or fails instantly. Caps must sum to less than
# HOLD_START_TIMEOUT_SECONDS so the foreground waiter still outlives both tries.
TUNNEL_USBMUX_TIMEOUT_SECONDS = 10
TUNNEL_WIFI_TIMEOUT_SECONDS = 45
CMD_TIMEOUT_SECONDS = 8
RSD_FETCH_RETRIES = 3
COMMAND_RETRIES = 3
RETRY_DELAY_SECONDS = 1.5
RSD_CONNECT_TIMEOUT_SECONDS = 2
DEFAULT_LOG_PATH = RUNTIME_DIR / "simlocation.log"
HOLD_START_TIMEOUT_SECONDS = 60
HOLD_POLL_INTERVAL_SECONDS = 0.25
# How often a moving session pushes a new coordinate. Also the resolution of
# the progress shown by `status`, so it is deliberately human-scaled rather
# than as fast as the DVT channel would allow.
ROUTE_UPDATE_INTERVAL_SECONDS = 1.0
MAX_ROUTE_POINTS = 10000
MAX_ROUTE_BYTES = 4 * 1024 * 1024
EARTH_RADIUS_METERS = 6371008.8
DEFAULT_ROUTE_SPEED_KMH = 5.0

DEFAULT_DEVICES_PATH = RUNTIME_DIR / "devices.json"
# Capture web job messages without redirecting stdout across HTTP threads.
LOG_CONTEXT = threading.local()


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
    write_state(devices_path, data)


def resolve_alias(name, devices_data):
    """Resolve an alias or UDID string to a UDID. Returns the input unchanged if not an alias."""
    return devices_data["aliases"].get(name, name)


def reverse_alias(udid, devices_data):
    """Find the alias for a UDID, or return None."""
    for alias, u in devices_data["aliases"].items():
        if u == udid:
            return alias
    return None


UDID_PATTERN = re.compile(r"^(?:[0-9A-Fa-f]{8}-[0-9A-Fa-f]{16}|[0-9A-Fa-f]{40})$")


def looks_like_udid(value):
    """Return True when value has the shape of a modern or legacy iOS UDID."""
    return bool(value) and UDID_PATTERN.match(value) is not None


def validate_coordinates(lat, lon):
    """Parse and range-check a lat/lon pair. Raises ValueError with a Chinese message."""
    # bool is an int subclass, so JSON `true` would otherwise sail through as 1.0.
    if isinstance(lat, bool) or isinstance(lon, bool):
        raise ValueError(f"坐标必须是数字，收到: {lat} {lon}")
    try:
        lat_value = float(lat)
        lon_value = float(lon)
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f"坐标必须是数字，收到: {lat} {lon}") from None
    if not math.isfinite(lat_value) or not math.isfinite(lon_value):
        raise ValueError(f"坐标必须是有限数字，收到: {lat} {lon}")
    if not (-90.0 <= lat_value <= 90.0):
        raise ValueError(f"纬度必须在 -90 到 90 之间，收到: {lat}")
    if not (-180.0 <= lon_value <= 180.0):
        raise ValueError(f"经度必须在 -180 到 180 之间，收到: {lon}")
    return lat_value, lon_value


def route_distance(start, end):
    """Great-circle distance in meters between two (lat, lon) pairs."""
    lat1, lon1, lat2, lon2 = map(math.radians, (*start, *end))
    a = (
        math.sin((lat2 - lat1) / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    )
    return 2 * EARTH_RADIUS_METERS * math.asin(math.sqrt(min(1.0, max(0.0, a))))


class Route:
    """A polyline measured in meters, interpolated along great-circle segments.

    Waypoints are kept in input order with consecutive duplicates dropped, so a
    double-click on the map picker does not create a zero-length segment that
    would divide by zero during interpolation.
    """

    def __init__(self, points, loop=False):
        if not isinstance(points, (list, tuple)) or not 2 <= len(points) <= MAX_ROUTE_POINTS:
            raise ValueError(f"路线需要 2 到 {MAX_ROUTE_POINTS} 个坐标点。")
        self.points = []
        for index, point in enumerate(points, 1):
            if isinstance(point, (str, bytes)) or not isinstance(point, (list, tuple)):
                raise ValueError(f"第 {index} 个点应为 [纬度, 经度]。")
            if len(point) != 2:
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
        self.loop = loop
        self.cumulative = [0.0]
        for start, end in zip(self.points, self.points[1:]):
            distance = route_distance(start, end)
            # Antipodal endpoints have infinitely many great circles through
            # them, so there is no single path to interpolate along.
            if distance / EARTH_RADIUS_METERS >= math.pi - 1e-6:
                raise ValueError("路线包含地球正对两端的点，请增加中间途经点。")
            self.cumulative.append(self.cumulative[-1] + distance)
        self.total_m = self.cumulative[-1]

    def waypoints(self):
        """Waypoints as stored on disk: the closing point of a loop is implied."""
        return self.points[:-1] if self.loop else self.points

    def position(self, distance_m):
        """Coordinates reached after travelling distance_m along the polyline."""
        if distance_m <= 0:
            return self.points[0]
        if distance_m >= self.total_m:
            return self.points[-1]
        index = bisect_right(self.cumulative, distance_m) - 1
        segment_m = self.cumulative[index + 1] - self.cumulative[index]
        fraction = (distance_m - self.cumulative[index]) / segment_m
        # Spherical linear interpolation: walking a straight line in lat/lon
        # drifts off the great circle and distorts badly at high latitudes.
        angle = segment_m / EARTH_RADIUS_METERS
        weights = (
            math.sin((1 - fraction) * angle) / math.sin(angle),
            math.sin(fraction * angle) / math.sin(angle),
        )
        vectors = []
        for lat, lon in self.points[index:index + 2]:
            lat, lon = math.radians(lat), math.radians(lon)
            vectors.append(
                (math.cos(lat) * math.cos(lon), math.cos(lat) * math.sin(lon), math.sin(lat))
            )
        x, y, z = (
            sum(weight * vector[axis] for weight, vector in zip(weights, vectors))
            for axis in range(3)
        )
        return math.degrees(math.atan2(z, math.hypot(x, y))), math.degrees(math.atan2(y, x))


def parse_gpx_points(data):
    """Extract the single track segment or route from a GPX document."""
    if xml_fromstring is not None:
        root = xml_fromstring(data)
    else:
        # No defusedxml: refuse any DTD rather than hand expat an entity bomb.
        if b"<!DOCTYPE" in data[:4096]:
            raise ValueError("GPX 不支持 DOCTYPE 声明。")
        root = ET.fromstring(data)
    segments = []
    for element in root.iter():
        tag = element.tag.rsplit("}", 1)[-1]
        if tag in ("trkseg", "rte"):
            point_tag = "trkpt" if tag == "trkseg" else "rtept"
            points = [
                (point.get("lat"), point.get("lon"))
                for point in element
                if point.tag.rsplit("}", 1)[-1] == point_tag
            ]
            if points:
                segments.append(points)
    if len(segments) != 1:
        raise ValueError("GPX 需要且只能包含一条连续的 track segment 或 route。")
    return segments[0]


def parse_route_data(data, file_format="json", loop=False):
    """Parse uploaded or local route bytes with the same limits and validation."""
    if len(data) > MAX_ROUTE_BYTES:
        raise ValueError(f"路线文件不能超过 {MAX_ROUTE_BYTES // (1024 * 1024)} MiB。")
    try:
        if file_format == "gpx":
            points = parse_gpx_points(data)
        elif file_format == "json":
            decoded = json.loads(data)
            points = decoded.get("points") if isinstance(decoded, dict) else decoded
        else:
            raise ValueError("仅支持 JSON 或 GPX 路线。")
        return Route(points, loop=loop)
    except (ValueError, ET.ParseError, RecursionError) as exc:
        raise ValueError(f"路线内容无效: {exc}") from exc


def load_route(path, loop=False):
    """Build a Route from a JSON waypoint list or a single-segment GPX file."""
    path = Path(path).expanduser()
    try:
        with path.open("rb") as handle:
            data = handle.read(MAX_ROUTE_BYTES + 1)
        return parse_route_data(data, "gpx" if path.suffix.lower() == ".gpx" else "json", loop)
    except (OSError, ValueError, ET.ParseError, RecursionError) as exc:
        raise ValueError(f"无法读取路线 {path.name}: {exc}") from exc


def pid_path_for(udid):
    return RUNTIME_DIR / f"{udid}.pid"


def state_path_for(udid):
    return RUNTIME_DIR / f"{udid}.state.json"


def log_message(message, log_path=None):
    print(message)
    sink = getattr(LOG_CONTEXT, "sink", None)
    if sink:
        sink(message)
    if not log_path:
        return
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(log_path, "a", encoding="utf-8") as handle:
        handle.write(f"[{timestamp}] {message}\n")


def write_state(state_path, payload):
    state_path.parent.mkdir(parents=True, exist_ok=True)
    # A moving session rewrites this once a second while `status` reads it, so
    # a plain write_text() would eventually be caught mid-flush and parsed as
    # truncated JSON. Rename is atomic within the directory.
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
    if not isinstance(pid, int) or pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes
        kernel32 = ctypes.windll.kernel32
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        ERROR_ACCESS_DENIED = 5
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if handle:
            kernel32.CloseHandle(handle)
            return True
        # A live process owned by another user is reported as access denied.
        return kernel32.GetLastError() == ERROR_ACCESS_DENIED
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


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


def _is_executable_file(path):
    return Path(path).is_file() and os.access(str(path), os.X_OK)


def sibling_pymobiledevice3_candidates(python_executable=None):
    """CLI paths that belong to the same environment as the running interpreter."""
    python_path = Path(python_executable or sys.executable).resolve()
    names = ["pymobiledevice3"]
    if sys.platform == "win32":
        names = ["pymobiledevice3.exe", "pymobiledevice3"]
    candidates = [python_path.with_name(name) for name in names]
    if sys.platform == "win32":
        # venv/conda interpreters sit beside Scripts/, and the base installer
        # also places console scripts under Scripts/.
        candidates.extend(python_path.parent / "Scripts" / name for name in names)
    return candidates


def resolve_pymobiledevice3(required=True):
    """Locate the pymobiledevice3 CLI.

    Order: SIMLOCATION_PMD3, the CLI installed next to the running Python
    (so module and CLI versions match), then PATH. With required=False the
    function returns None instead of exiting when nothing is found.
    """
    override = os.environ.get("SIMLOCATION_PMD3")
    if override:
        if _is_executable_file(override):
            return override
        print(f"环境变量 SIMLOCATION_PMD3 指向的文件不可执行: {override}")
        sys.exit(1)

    for candidate in sibling_pymobiledevice3_candidates():
        if _is_executable_file(candidate):
            return str(candidate)

    found = shutil.which("pymobiledevice3")
    if found:
        return found

    if not required:
        return None
    print("未找到 pymobiledevice3 可执行文件。")
    print(
        "请安装 pymobiledevice3 后重试，或设置环境变量 SIMLOCATION_PMD3 指向其完整路径。"
    )
    sys.exit(1)


def _pick_addr_port(record):
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


def extract_rsd_pairs(data):
    """Return every (address, port) pair found in one device's tunneld record(s).

    tunneld lists a device as a list of tunnel dicts; a single dict is also
    accepted. Order is preserved and duplicates are dropped.
    """
    records = data if isinstance(data, list) else [data]
    pairs = []
    for record in records:
        matched = _pick_addr_port(record)
        if matched and matched not in pairs:
            pairs.append(matched)
    return pairs


def get_tunneld_snapshot(log_path=None):
    response = requests.get(TUNNELD_URL, timeout=TUNNELD_REQUEST_TIMEOUT_SECONDS)
    response.raise_for_status()
    return response.json()


def snapshot_rsd_candidates(udid, log_path=None, retries=RSD_FETCH_RETRIES):
    """Return the RSD pairs tunneld currently exposes for exactly this UDID.

    Returns a (possibly empty) list when tunneld answered, and None when tunneld
    could not be queried at all. Tunnels registered under other UDIDs are never
    used as a fallback.
    """
    last_error = None
    for attempt in range(1, retries + 1):
        try:
            data = get_tunneld_snapshot(log_path)
        except Exception as exc:
            last_error = f"无法获取 tunneld 状态，请检查 tunneld 是否正在运行: {exc}"
        else:
            if not isinstance(data, dict):
                last_error = f"tunneld 返回了无法识别的内容: {data}"
            elif udid not in data:
                log_message(f"[*] tunneld 中尚无设备 {udid} 的 tunnel。", log_path)
                return []
            else:
                candidates = extract_rsd_pairs(data[udid])
                if candidates:
                    listed = ", ".join(f"{addr} {port}" for addr, port in candidates)
                    log_message(f"[*] tunneld 中设备 {udid} 的 RSD: {listed}", log_path)
                    return candidates
                last_error = f"tunneld 中设备 {udid} 的记录缺少地址/端口: {data[udid]}"

        if attempt < retries:
            log_message(
                f"[!] 获取 RSD 失败，{RETRY_DELAY_SECONDS} 秒后重试 ({attempt}/{retries})。",
                log_path,
            )
            time.sleep(RETRY_DELAY_SECONDS)

    if last_error:
        log_message(f"[!] {last_error}", log_path)
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
    except Exception as exc:
        if log_path:
            log_message(f"[!] 从 tunneld 发现设备失败: {exc}", log_path)
    try:
        result = None
        # --no-color is a group-level option: `usbmux list --no-color` is
        # rejected outright by pymobiledevice3 9.12+, which used to make this
        # whole fallback dead whenever tunneld was unreachable. Older builds
        # that predate the group option still need the bare form.
        for argv in ([pmd3_bin, "--no-color", "usbmux", "list"],
                     [pmd3_bin, "usbmux", "list"]):
            result = subprocess.run(
                argv, capture_output=True, text=True, timeout=CMD_TIMEOUT_SECONDS,
            )
            if result.returncode == 0:
                break
        if result.returncode == 0:
            devices = json.loads(result.stdout)
            if isinstance(devices, list):
                for dev in devices:
                    if isinstance(dev, dict) and dev.get("UniqueDeviceID"):
                        udids.add(dev["UniqueDeviceID"])
        elif log_path:
            log_message(
                f"[!] usbmux list 退出码 {result.returncode}: {result.stderr.strip()}",
                log_path,
            )
    except Exception as exc:
        if log_path:
            log_message(f"[!] 通过 usbmux 发现设备失败: {exc}", log_path)
    return sorted(udids)


def interactive_device_select(udids, devices_data, log_path=None):
    """Prompt user to select a device from a list. Returns UDID."""
    if not sys.stdin.isatty():
        log_message(
            "[!] 检测到多台设备，但当前不是交互终端，无法选择。"
            "请使用 --device 或 SIMLOCATION_UDID 指定设备。",
            log_path,
        )
        sys.exit(1)
    print("[?] 检测到多台设备，请选择：")
    for i, udid in enumerate(udids, 1):
        alias = reverse_alias(udid, devices_data)
        label = f"{alias} ({udid})" if alias else udid
        print(f"  {i}. {label}")
    while True:
        try:
            choice = input("请输入序号: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            print("[!] 已取消设备选择。")
            sys.exit(1)
        try:
            idx = int(choice) - 1
        except ValueError:
            idx = -1
        if 0 <= idx < len(udids):
            return udids[idx]
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
        if udid == device_flag and not looks_like_udid(udid):
            known = ", ".join(sorted(devices_data["aliases"])) or "无"
            log_message(
                f"[!] 未知的设备别名: {device_flag}（已注册别名: {known}）。"
                "请先执行 simlocation device add，或直接传入完整 UDID。",
                log_path,
            )
            sys.exit(1)
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


# Tried in order; each attempt creates at most one tunnel task inside tunneld.
_REQUEST_CONNECTION_ORDER = (
    ("usbmux", TUNNEL_USBMUX_TIMEOUT_SECONDS),
    ("wifi", TUNNEL_WIFI_TIMEOUT_SECONDS),
)


def request_fresh_rsd(udid, log_path=None):
    """Ask tunneld to create (or hand back) a tunnel for udid.

    tunneld's /start-tunnel with an explicit connection_type tries exactly one
    transport, so usbmux is attempted first (it answers in fractions of a second
    for a USB device and fails instantly otherwise) and Wi-Fi second. The plain
    multi-transport request is deliberately not used: on a dead tunnel it burns
    its whole timeout inside a bonjour scan. A timed-out attempt is never
    re-requested; the tunnel task keeps running inside tunneld, so the snapshot
    is consulted instead - a later attempt would find it registered.
    """
    for connection_type, timeout_seconds in _REQUEST_CONNECTION_ORDER:
        log_message(
            f"[*] 正在请求 tunneld 为设备 {udid} 建立 tunnel（{connection_type}，最多等待 {timeout_seconds} 秒）...",
            log_path,
        )
        try:
            response = requests.get(
                f"{TUNNELD_URL}/start-tunnel",
                params={"udid": udid, "connection_type": connection_type},
                timeout=timeout_seconds,
            )
            response.raise_for_status()
            data = response.json()
        except requests.Timeout:
            log_message(
                f"[!] 等待 tunneld 建立 {connection_type} tunnel 超过 {timeout_seconds} 秒，改为检查 tunneld 是否已在后台完成。",
                log_path,
            )
            candidates = snapshot_rsd_candidates(udid, log_path, retries=1) or []
            if candidates:
                return candidates[0]
            log_message(
                f"[!] 设备 {udid} 的 {connection_type} tunnel 未能在 {timeout_seconds} 秒内建立，尝试下一种传输方式。",
                log_path,
            )
            continue
        except requests.HTTPError as exc:
            body = ""
            if exc.response is not None:
                body = f" {exc.response.status_code}: {(exc.response.text or '').strip()[:200]}"
            log_message(f"[!] tunneld /start-tunnel ({connection_type}) 返回错误{body}", log_path)
            continue
        except requests.RequestException as exc:
            log_message(f"[!] 请求 {connection_type} tunnel 失败: {exc}", log_path)
            continue
        except ValueError as exc:
            log_message(f"[!] tunneld /start-tunnel 返回了无法解析的内容: {exc}", log_path)
            continue
        else:
            address = data.get("address") if isinstance(data, dict) else None
            port = data.get("port") if isinstance(data, dict) else None
            if address and port:
                log_message(
                    f"[*] tunneld 已为设备 {udid} 提供 tunnel: {address} {port}（{connection_type}）",
                    log_path,
                )
                return str(address), str(port)
            log_message(f"[!] tunneld /start-tunnel ({connection_type}) 返回异常: {data}", log_path)

    failures = "、".join(name for name, _ in _REQUEST_CONNECTION_ORDER)
    log_message(
        f"[!] tunneld 未能通过 {failures} 为设备 {udid} 建立 tunnel，请确认设备已解锁并连接。",
        log_path,
    )
    return None


def cancel_tunnel(udid, log_path=None):
    """Ask tunneld to drop the tunnels it has registered for udid.

    tunneld's /start-tunnel hands back whatever tunnel is registered for the
    UDID without checking that it still works, so a dead-but-registered tunnel
    can only be replaced after /cancel removes it. Only call this after every
    registered tunnel of the device failed the reachability probe.
    """
    try:
        response = requests.get(
            f"{TUNNELD_URL}/cancel",
            params={"udid": udid},
            timeout=TUNNELD_REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        log_message(f"[!] 请求 tunneld 取消失效 tunnel 失败: {exc}", log_path)
        return False
    log_message(f"[*] 已请求 tunneld 取消设备 {udid} 的失效 tunnel。", log_path)
    return True


def acquire_rsd(udid, connection_mode="auto", log_path=None):
    """Pick a reachable RSD for udid: reuse tunneld's existing tunnels first.

    In "rsd" mode only existing tunnels are considered. In "auto" mode, when
    none of the existing tunnels is reachable they are cancelled (tunneld would
    otherwise keep handing them back) and a new tunnel is requested.
    """
    candidates = snapshot_rsd_candidates(udid, log_path)
    if candidates is None:
        return None

    for rsd_pair in candidates:
        if is_rsd_reachable(rsd_pair[0], rsd_pair[1], log_path):
            log_message(
                f"[*] 复用 tunneld 中已有的 RSD: {rsd_pair[0]} {rsd_pair[1]}",
                log_path,
            )
            return rsd_pair

    if connection_mode == "rsd":
        if candidates:
            log_message("[!] tunneld 中现有的 RSD 均不可达；--connection rsd 模式不会创建新 tunnel。", log_path)
        else:
            log_message("[!] tunneld 中没有该设备的 tunnel；--connection rsd 模式不会创建新 tunnel。", log_path)
        return None

    if candidates:
        log_message("[!] tunneld 中现有的 RSD 均不可达，先请求 tunneld 取消它们，再重建 tunnel。", log_path)
        cancel_tunnel(udid, log_path)
    rsd_pair = request_fresh_rsd(udid, log_path)
    if not rsd_pair:
        return None
    if is_rsd_reachable(rsd_pair[0], rsd_pair[1], log_path):
        return rsd_pair
    log_message(
        f"[!] tunneld 新建的 RSD {rsd_pair[0]} {rsd_pair[1]} 仍不可达，请确认设备已解锁并连接；反复失败时重启 tunneld。",
        log_path,
    )
    return None


def is_rsd_reachable(host, port, log_path=None, quiet=False):
    """TCP-probe an RSD endpoint. With quiet=True nothing is printed (doctor builds its own report)."""

    def note(message):
        if not quiet:
            log_message(message, log_path)

    try:
        addrinfos = socket.getaddrinfo(host, int(port), type=socket.SOCK_STREAM)
    except Exception as exc:
        note(f"[!] 解析 RSD 地址失败: {host}:{port} ({exc})")
        return False

    last_error = None
    for family, socktype, proto, _, sockaddr in addrinfos:
        sock = None
        try:
            sock = socket.socket(family, socktype, proto)
            sock.settimeout(RSD_CONNECT_TIMEOUT_SECONDS)
            sock.connect(sockaddr)
            note(f"[*] RSD 端口可达: {host}:{port}")
            return True
        except Exception as exc:
            last_error = exc
        finally:
            if sock is not None:
                sock.close()

    note(f"[!] RSD 端口不可达: {host}:{port} ({last_error})")
    return False


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


async def clear_held_location(simulation, state_path):
    state = read_state(state_path) or {}
    state["status"] = "clearing"
    state["clear_confirmed"] = False
    write_state(state_path, state)
    try:
        await simulation.clear()
    except Exception as exc:
        state["status"] = "error"
        state["clear_confirmed"] = False
        state["clear_error"] = str(exc)
        state["failed_at"] = datetime.now().isoformat(timespec="seconds")
        write_state(state_path, state)
        raise

    state["status"] = "cleared"
    state["clear_confirmed"] = True
    state["cleared_at"] = datetime.now().isoformat(timespec="seconds")
    state.pop("clear_error", None)
    write_state(state_path, state)


async def play_route(
    simulation, route, speed_kmh, loop_route, stop_event, state, state_path,
    clock=time.monotonic,
):
    """Walk the route at speed_kmh, pushing a coordinate every tick.

    Position is derived from elapsed wall time rather than accumulated per-tick
    steps, so a slow DVT round-trip makes the next update jump ahead instead of
    letting the simulated device fall progressively behind schedule.
    """
    started = clock()
    while not stop_event.is_set():
        traveled_m = (clock() - started) * speed_kmh / 3.6
        distance_m = traveled_m % route.total_m if loop_route else min(traveled_m, route.total_m)
        lat, lon = route.position(distance_m)
        await simulation.set(lat, lon)
        completed = not loop_route and traveled_m >= route.total_m
        state.update(
            # ~0.1 m of precision: the state file is read by humans and by
            # `status`, and full float repr makes the device table unreadable.
            lat=f"{lat:.6f}",
            lon=f"{lon:.6f}",
            route_phase="completed" if completed else "moving",
            distance_m=distance_m,
            progress=distance_m / route.total_m,
            lap=int(traveled_m // route.total_m) + 1 if loop_route else 1,
        )
        write_state(state_path, state)
        if completed:
            # Hold the endpoint rather than dropping the simulation, so the
            # device does not snap back to its real location on arrival.
            await stop_event.wait()
            return
        with suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop_event.wait(), timeout=ROUTE_UPDATE_INTERVAL_SECONDS)


async def _hold_dvt_location_session(
    rsd_pair, lat, lon, state_path, log_path=None, stop_event=None,
    *, route=None, speed_kmh=DEFAULT_ROUTE_SPEED_KMH, loop_route=False,
):
    if stop_event is None:
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
                if stop_event.is_set():
                    log_message("[*] 启动期间收到停止请求，未设置定位。", log_path)
                    return
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
                    state.update(
                        mode="route",
                        route_phase="moving",
                        speed_kmh=speed_kmh,
                        loop=loop_route,
                        total_m=route.total_m,
                        distance_m=0.0,
                        progress=0.0,
                        lap=1,
                    )
                write_state(state_path, state)
                try:
                    if route:
                        log_message(
                            f"[+] 后台运动轨迹会话已建立，{route.total_m:.0f} m / "
                            f"{speed_kmh:g} km/h，直到执行 clear。",
                            log_path,
                        )
                        await play_route(
                            simulation, route, speed_kmh, loop_route,
                            stop_event, state, state_path,
                        )
                    else:
                        log_message(
                            "[+] 后台定位会话已建立，将持续保持当前位置直到执行 clear。",
                            log_path,
                        )
                        await stop_event.wait()
                except BaseException:
                    # A moving session can fail mid-flight (device unplugged,
                    # DVT error). Dropping out without clearing would strand the
                    # device at a fake location, so try once before propagating.
                    # Best-effort only: the same fault usually breaks clear too.
                    with suppress(Exception):
                        await simulation.clear()
                    raise
                await clear_held_location(simulation, state_path)


def run_hold_session(
    lat, lon, pmd3_bin, connection_mode, pid_path, state_path, log_path=None,
    *, route=None, speed_kmh=DEFAULT_ROUTE_SPEED_KMH, loop_route=False,
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
            _hold_dvt_location_session(
                rsd_pair, lat, lon, state_path, log_path,
                route=route, speed_kmh=speed_kmh, loop_route=loop_route,
            )
        )
        state = read_state(state_path) or {}
        state["status"] = "stopped"
        state["stopped_at"] = datetime.now().isoformat(timespec="seconds")
        write_state(state_path, state)
    except Exception as exc:
        state = read_state(state_path) or {}
        state.update(
            {
                "status": "error",
                "pid": os.getpid(),
                "error": str(exc),
                "failed_at": datetime.now().isoformat(timespec="seconds"),
            }
        )
        write_state(state_path, state)
        log_message(f"[-] 后台定位会话启动失败: {exc}", log_path)
        raise
    finally:
        remove_file_if_exists(pid_path)


def start_hold_session(
    lat, lon, pmd3_bin, connection_mode, udid, log_path=None,
    *, route_file=None, speed_kmh=DEFAULT_ROUTE_SPEED_KMH, loop_route=False,
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
    if route_file:
        cmd.extend(["route", str(route_file), "--speed", repr(float(speed_kmh))])
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
    try:
        return wait_for_hold_session(proc, state_path, timeout_seconds, log_path)
    finally:
        # The web console outlives its children. Reap every session when it
        # exits so a zombie cannot look alive to stop_hold_session(). Daemon
        # threads let short-lived CLI commands still exit while sessions run.
        threading.Thread(target=proc.wait, daemon=True).start()


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


def auto_set_route(
    route, speed_kmh, loop_route, pmd3_bin, connection_mode="auto", log_path=None,
    udid=None, device_flag=None,
):
    if not udid:
        udid = resolve_device_udid(pmd3_bin, log_path, device_flag=device_flag)
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    # Hand the worker an immutable snapshot: the source file may be edited or
    # deleted while the session runs, and a route drawn on the map has no file
    # at all. The worker loads it during argument parsing, before it reports
    # ready, so the snapshot can be removed as soon as start_hold_session returns.
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", prefix="route-", dir=RUNTIME_DIR,
        encoding="utf-8", delete=False,
    ) as handle:
        snapshot = Path(handle.name)
        json.dump({"points": route.waypoints()}, handle)
    try:
        message = (
            f"[*] 正在启动运动轨迹，连接模式: {connection_mode}，设备: {udid}。\n"
            f"[*] 全程 {route.total_m:.0f} m，{speed_kmh:g} km/h，"
            f"{'循环移动' if loop_route else '到终点后保持定位'}。"
        )
        log_message(message, log_path)
        if start_hold_session(
            *route.points[0], pmd3_bin, connection_mode, udid, log_path,
            route_file=snapshot, speed_kmh=speed_kmh, loop_route=loop_route,
        ):
            log_message("[+] 运动轨迹已启动，用 status 查看进度，clear 结束。", log_path)
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
    stopped_session = stop_hold_session(
        pid_path, state_path, log_path, quiet=False
    )
    state = read_state(state_path)
    if stopped_session and state and state.get("clear_confirmed"):
        log_message("[+] 已通过后台定位会话清除虚拟定位。", log_path)
        return

    for attempt in range(1, COMMAND_RETRIES + 1):
        rsd_pair = acquire_rsd(udid, connection_mode, log_path)
        if not rsd_pair:
            log_message("未找到有效的 RSD 隧道，无法清除定位。", log_path)
            sys.exit(1)

        log_message(
            f"[*] 正在通过 RSD {rsd_pair[0]} {rsd_pair[1]} 清除设备 {udid} 的虚拟定位 ({attempt}/{COMMAND_RETRIES})。",
            log_path,
        )
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
MAP_MAX_BODY_BYTES = 4096
MAP_ROUTE_SCRIPT_PATH = PROJECT_DIR / "web" / "map-route.js"
MAP_DEFAULT_LISTEN = "127.0.0.1"


def get_map_timeout_seconds():
    """How long the picker stays open. Remote use needs more than local use."""
    raw = os.environ.get("SIMLOCATION_MAP_TIMEOUT_SECONDS", "").strip()
    if raw:
        try:
            value = int(raw)
            if value > 0:
                return value
        except ValueError:
            pass
    return 300


MAP_SERVER_TIMEOUT_SECONDS = get_map_timeout_seconds()


def is_loopback_host(host):
    """Loopback binds are private; anything else is reachable by other hosts.

    "" is deliberately not loopback: Python binds it to every interface.
    """
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def resolve_map_listen_host(listen_host=None):
    """Explicit value wins over SIMLOCATION_MAP_LISTEN; blanks stay private.

    A blank --listen or variable would otherwise reach HTTPServer as "" and
    silently bind every interface without a token.
    """
    if listen_host is None:
        listen_host = os.environ.get("SIMLOCATION_MAP_LISTEN", "")
    return listen_host.strip() or MAP_DEFAULT_LISTEN


def guess_reachable_hosts():
    """Best-effort list of addresses a phone on the same network could use."""
    hosts = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            addr = info[4][0]
            if addr not in hosts and not addr.startswith("127."):
                hosts.append(addr)
    except (socket.gaierror, OSError):
        pass
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.settimeout(0.2)
        try:
            probe.connect(("192.0.2.1", 9))
            addr = probe.getsockname()[0]
            if addr not in hosts and not addr.startswith("127."):
                hosts.append(addr)
        finally:
            probe.close()
    except OSError:
        pass
    return hosts


class _MapRequestHandler(BaseHTTPRequestHandler):
    def _route(self):
        parts = urlsplit(self.path)
        return parts.path, parse_qs(parts.query)

    def _authorized(self, query):
        """No token means loopback-only; otherwise the URL must carry it."""
        token = getattr(self.server, "access_token", None)
        if not token:
            return True
        supplied = (query.get("t") or [""])[0]
        # compare_digest() rejects non-ASCII str with TypeError, which would
        # turn a stray "?t=中文" into a traceback and a dropped connection.
        return secrets.compare_digest(supplied.encode("utf-8"), token.encode("utf-8"))

    def do_GET(self):
        path, query = self._route()
        if path != "/":
            self.send_error(404)
            return
        if not self._authorized(query):
            self.send_error(403)
            return
        html = self.server.map_html
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(html)))
        self.end_headers()
        self.wfile.write(html)

    def do_POST(self):
        path, query = self._route()
        if path != "/confirm":
            self.send_error(404)
            return
        if not self._authorized(query):
            self.send_error(403)
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            self.send_error(400)
            return
        if length <= 0:
            self.send_error(400)
            return
        # A single coordinate is a few dozen bytes; a 10k-waypoint route is
        # hundreds of KB, so the cap has to follow the mode the picker runs in.
        route_mode = getattr(self.server, "route_mode", False)
        max_bytes = MAX_ROUTE_BYTES if route_mode else MAP_MAX_BODY_BYTES
        if length > max_bytes:
            self.send_error(413)
            return
        body = self.rfile.read(length)
        try:
            data = json.loads(body)
            if route_mode:
                # Build the Route here: rejecting a bad polyline with 400 lets
                # the browser keep the drawing, where shutting the picker down
                # first would lose it.
                picked = Route(data["points"], loop=getattr(self.server, "loop_route", False))
            else:
                picked = validate_coordinates(data["lat"], data["lon"])
        except (json.JSONDecodeError, KeyError, ValueError, TypeError):
            self.send_error(400)
            return
        self.server.picked_coords = picked
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        resp = b'{"ok":true}'
        self.send_header("Content-Length", str(len(resp)))
        self.end_headers()
        self.wfile.write(resp)
        threading.Thread(target=self.server.shutdown, daemon=True).start()

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


def render_map_html(
    amap_key=None, *, route_mode=False, speed_kmh=DEFAULT_ROUTE_SPEED_KMH,
    loop_route=False, pick_only=False,
):
    html_path = MAP_AMAP_HTML_PATH if amap_key else MAP_OSM_HTML_PATH
    template = html_path.read_text(encoding="utf-8")
    html_text = template
    # The route editor is shared by both providers, so it lives in its own file
    # and is inlined here rather than served as a second endpoint.
    html_text = html_text.replace(
        "{{ROUTE_SCRIPT}}", MAP_ROUTE_SCRIPT_PATH.read_text(encoding="utf-8")
    )
    for placeholder, value in (
        ("{{ROUTE_MODE}}", "true" if route_mode else "false"),
        ("{{ROUTE_LOOP}}", "true" if loop_route else "false"),
        ("{{PICK_ONLY}}", "true" if pick_only else "false"),
        ("{{ROUTE_SPEED}}", repr(float(speed_kmh))),
        ("{{MAX_ROUTE_POINTS}}", str(MAX_ROUTE_POINTS)),
    ):
        html_text = html_text.replace(placeholder, value)
    if amap_key:
        html_text = html_text.replace("{{AMAP_KEY}}", quote(amap_key, safe=""))
    return html_text.encode("utf-8")


def run_map_picker(
    amap_key=None, listen_host=None, listen_port=None, open_browser=True,
    *, route_mode=False, speed_kmh=DEFAULT_ROUTE_SPEED_KMH, loop_route=False,
    pick_only=False,
):
    if amap_key:
        html_path = MAP_AMAP_HTML_PATH
        provider = "高德地图"
    else:
        html_path = MAP_OSM_HTML_PATH
        provider = "OpenStreetMap"

    for required in (html_path, MAP_ROUTE_SCRIPT_PATH):
        if not required.is_file():
            print(f"[!] 地图页面文件不存在: {required}")
            sys.exit(1)
    listen_host = resolve_map_listen_host(listen_host)
    if listen_port is None:
        raw_port = os.environ.get("SIMLOCATION_MAP_PORT", "").strip()
        try:
            listen_port = int(raw_port) if raw_port else 0
        except ValueError:
            listen_port = 0

    exposed = not is_loopback_host(listen_host)

    try:
        server = HTTPServer((listen_host, listen_port), _MapRequestHandler)
    except OSError as exc:
        print(f"[!] 无法在 {listen_host}:{listen_port} 启动地图服务: {exc}")
        sys.exit(1)
    port = server.server_address[1]

    server.map_html = render_map_html(
        amap_key, route_mode=route_mode, speed_kmh=speed_kmh,
        loop_route=loop_route, pick_only=pick_only,
    )
    server.picked_coords = None
    server.route_mode = route_mode
    server.loop_route = loop_route
    # A non-loopback bind is reachable by every host on the network, so the
    # URL itself becomes the credential. A fixed token can be supplied so an
    # always-on host keeps a bookmarkable URL across restarts.
    if exposed:
        server.access_token = (
            os.environ.get("SIMLOCATION_MAP_TOKEN", "").strip() or secrets.token_urlsafe(16)
        )
    else:
        server.access_token = None

    query = f"?t={server.access_token}" if server.access_token else ""
    print(f"[*] 地图选点服务已启动 ({provider})，{MAP_SERVER_TIMEOUT_SECONDS} 秒后超时。")
    if exposed:
        display_hosts = guess_reachable_hosts() if listen_host == "0.0.0.0" else [listen_host]
        if not display_hosts:
            display_hosts = [listen_host]
        action = "依次添加途经点后确认路线" if route_mode else "选点后点「确认位置」"
        print(f"[*] 请在手机浏览器打开下面任意一个地址，{action}：")
        for host in display_hosts:
            print(f"      http://{host}:{port}/{query}")
        if os.environ.get("SIMLOCATION_MAP_TOKEN", "").strip():
            print("[*] 该地址使用 SIMLOCATION_MAP_TOKEN 指定的固定令牌，可加入书签。")
        else:
            print("[*] 该地址包含一次性访问令牌，选点完成或超时后立即失效。")
        # Remote use almost always redirects stdout (systemd, nohup, ssh pipe),
        # where block buffering would hide the URL until the server exits.
        sys.stdout.flush()
    else:
        url = f"http://{listen_host}:{port}/"
        print(f"[*] {url}")
        if open_browser:
            print(
                "[*] 正在打开浏览器，请在地图上依次添加途经点后确认路线。" if route_mode
                else "[*] 正在打开浏览器，请在地图上选择位置后点击「确认」。"
            )
            _open_app_window(url)

    timer = threading.Timer(MAP_SERVER_TIMEOUT_SECONDS, server.shutdown)
    timer.daemon = True
    timer.start()

    try:
        server.serve_forever()
    finally:
        timer.cancel()
        server.server_close()
    return server.picked_coords


def add_common_options(parser, for_subcommand=False):
    """Attach options that are valid both before and after the subcommand.

    Subcommand copies use SUPPRESS defaults so they never overwrite a value
    that was already parsed at the top level (argparse copies subparser
    defaults back into the main namespace).
    """

    def default(value):
        return argparse.SUPPRESS if for_subcommand else value

    parser.add_argument(
        "--debug",
        action="store_true",
        default=default(False),
        help="记录详细日志到文件，便于排查热点场景下的不稳定问题",
    )
    parser.add_argument(
        "--log-file",
        default=default(str(DEFAULT_LOG_PATH)),
        help="调试日志文件路径，默认写到项目目录下的 var/simlocation.log",
    )
    parser.add_argument(
        "--connection",
        choices=("auto", "rsd"),
        default=default("auto"),
        help="连接模式。auto 优先复用 tunneld 中可达的 RSD，不可达时才请求新 tunnel；rsd 只复用现有 RSD，不创建。",
    )
    parser.add_argument(
        "--device", "-d",
        default=default(None),
        help="目标设备（别名或 UDID）",
    )


def add_map_server_options(parser):
    """Bind options for anything that serves the browser map picker."""
    parser.add_argument(
        "--listen",
        default=None,
        metavar="HOST",
        help="网页服务监听地址，默认 127.0.0.1。设为 0.0.0.0 可让同网络的手机访问（会自动启用访问令牌）",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="地图服务端口，默认随机。远程使用时建议固定",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="不尝试打开本机浏览器，只打印地址（无头主机上使用）",
    )
    parser.add_argument(
        "--remote",
        action="store_true",
        help="远程模式快捷方式，等价于 --listen 0.0.0.0 --no-browser",
    )


def map_bind_from_args(args):
    """Resolve (listen_host, open_browser) from the shared map server options."""
    listen_host = getattr(args, "listen", None)
    open_browser = not getattr(args, "no_browser", False)
    if getattr(args, "remote", False):
        listen_host = listen_host or "0.0.0.0"
        open_browser = False
    return listen_host, open_browser


def read_project_version():
    try:
        return (PROJECT_DIR / "VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        return "unknown"


def build_parser():
    parser = argparse.ArgumentParser(
        prog="simlocation",
        description="通过 pymobiledevice3 自动设置 iPhone 虚拟定位。",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {read_project_version()}",
    )
    add_common_options(parser)
    parser.add_argument("--_hold-session", action="store_true", help=argparse.SUPPRESS)
    # Internal: start_hold_session() always passes both paths explicitly.
    parser.add_argument("--pid-file", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--state-file", default=None, help=argparse.SUPPRESS)
    # Backward compat: --clear flag (legacy)
    parser.add_argument(
        "--clear",
        action="store_true",
        help=argparse.SUPPRESS,
    )

    subparsers = parser.add_subparsers(dest="command")

    # simlocation set <lat> <lon>
    sub_set = subparsers.add_parser("set", help="设置虚拟定位")
    add_common_options(sub_set, for_subcommand=True)
    sub_set.add_argument("lat", help="纬度")
    sub_set.add_argument("lon", help="经度")

    # simlocation route [file] [--speed N] [--loop]
    sub_route = subparsers.add_parser("route", help="沿路线模拟移动")
    add_common_options(sub_route, for_subcommand=True)
    sub_route.add_argument(
        "file", nargs="?", help="JSON 或 GPX 路线文件；省略则打开地图绘制路线"
    )
    sub_route.add_argument(
        "--speed", type=float, default=DEFAULT_ROUTE_SPEED_KMH,
        help=f"移动速度，单位 km/h，默认 {DEFAULT_ROUTE_SPEED_KMH:g}（约等于步行）",
    )
    sub_route.add_argument(
        "--loop", action="store_true", help="到终点后连回起点，循环移动",
    )
    sub_route.add_argument(
        "--pick-only", action="store_true",
        help="仅在地图上绘制并输出路线 JSON，不设置定位",
    )
    add_map_server_options(sub_route)

    # simlocation clear
    sub_clear = subparsers.add_parser("clear", help="清除虚拟定位，恢复真实位置")
    add_common_options(sub_clear, for_subcommand=True)
    sub_clear.add_argument("--all", action="store_true", dest="clear_all", help="清除所有设备的虚拟定位")

    # simlocation map [--pick-only]
    sub_map = subparsers.add_parser("map", help="打开地图选点，选择后自动设置定位")
    add_common_options(sub_map, for_subcommand=True)
    sub_map.add_argument(
        "--pick-only",
        action="store_true",
        help="仅选点并输出坐标，不自动设置定位",
    )
    add_map_server_options(sub_map)

    # Persistent browser console; one-shot map/route commands remain available.
    sub_web = subparsers.add_parser("web", help="打开常驻网页控制台")
    add_common_options(sub_web, for_subcommand=True)
    add_map_server_options(sub_web)

    # simlocation status
    sub_status = subparsers.add_parser("status", help="查看所有设备定位状态")
    add_common_options(sub_status, for_subcommand=True)

    # simlocation doctor
    sub_doctor = subparsers.add_parser("doctor", help="只读检查运行环境、tunneld 和设备连接")
    add_common_options(sub_doctor, for_subcommand=True)

    # simlocation device {list,add,remove,default}
    sub_device = subparsers.add_parser("device", help="设备管理")
    add_common_options(sub_device, for_subcommand=True)
    device_subparsers = sub_device.add_subparsers(dest="device_command")

    sub_device_list = device_subparsers.add_parser("list", help="列出所有设备")
    add_common_options(sub_device_list, for_subcommand=True)

    sub_device_add = device_subparsers.add_parser("add", help="注册设备别名")
    add_common_options(sub_device_add, for_subcommand=True)
    sub_device_add.add_argument("alias", help="设备别名")
    sub_device_add.add_argument("udid", nargs="?", default=None, help="设备 UDID（省略则交互选择）")

    sub_device_remove = device_subparsers.add_parser("remove", help="删除设备别名")
    add_common_options(sub_device_remove, for_subcommand=True)
    sub_device_remove.add_argument("alias", help="要删除的别名")

    sub_device_default = device_subparsers.add_parser("default", help="设置或查看默认设备")
    add_common_options(sub_device_default, for_subcommand=True)
    sub_device_default.add_argument("name", nargs="?", default=None, help="别名或 UDID（省略则查看当前默认）")

    return parser


def parse_legacy_args(raw):
    """Parse the pre-subcommand syntax: simlocation [options] <lat> <lon> | --clear."""
    legacy_parser = argparse.ArgumentParser(add_help=False)
    legacy_parser.add_argument("--clear", action="store_true")
    legacy_parser.add_argument("--debug", action="store_true")
    legacy_parser.add_argument("--log-file", default=str(DEFAULT_LOG_PATH))
    legacy_parser.add_argument("--connection", choices=("auto", "rsd"), default="auto")
    legacy_parser.add_argument("--device", "-d", default=None)
    legacy_parser.add_argument("--_hold-session", action="store_true")
    legacy_parser.add_argument("--pid-file", default=None)
    legacy_parser.add_argument("--state-file", default=None)
    args, positional = legacy_parser.parse_known_args(raw)
    args.command = None
    return args, positional


def env_default_coordinates():
    env_lat = os.environ.get("SIMLOCATION_DEFAULT_LAT")
    env_lon = os.environ.get("SIMLOCATION_DEFAULT_LON")
    if env_lat is not None and env_lon is not None:
        return env_lat, env_lon
    return None


def parse_args(argv=None):
    if argv is None:
        argv = sys.argv[1:]
    parser = build_parser()

    # Try normal parse first; if it fails on subcommand matching,
    # fall back to legacy positional arg handling.
    try:
        with open(os.devnull, "w") as null_stderr, redirect_stderr(null_stderr):
            args, remaining = parser.parse_known_args(argv)
    except SystemExit as e:
        if e.code == 0:
            # --help / --version triggered a clean exit
            sys.exit(0)
        # argparse exits on error — intercept to handle legacy format.
        args, remaining = parse_legacy_args(argv)

    if args.command is not None and remaining:
        parser.error(f"无法识别的参数: {' '.join(remaining)}")

    # Backward compat: simlocation <lat> <lon> (no subcommand)
    if args.command is None and not args.clear and not args._hold_session:
        if len(remaining) >= 2:
            args.command = "set"
            args.lat, args.lon = remaining[0], remaining[1]
            remaining = remaining[2:]
            if remaining:
                parser.error(f"无法识别的参数: {' '.join(remaining)}")
        elif len(remaining) == 0:
            coords = env_default_coordinates()
            if coords is None:
                parser.print_help()
                sys.exit(1)
            args.command = "set"
            args.lat, args.lon = coords
        else:
            parser.error(f"无法识别的参数: {' '.join(remaining)}")

    # Backward compat: --clear flag
    if args.clear and args.command is None:
        args.command = "clear"

    # _hold-session needs lat/lon
    if args._hold_session and args.command is None:
        if len(remaining) >= 2:
            args.command = "set"
            args.lat, args.lon = remaining[0], remaining[1]
        else:
            coords = env_default_coordinates()
            if coords is None:
                parser.error("_hold-session 需要坐标参数。")
            args.command = "set"
            args.lat, args.lon = coords

    if args.command == "set":
        try:
            validate_coordinates(args.lat, args.lon)
        except ValueError as exc:
            parser.error(str(exc))

    if args.command == "route":
        if not math.isfinite(args.speed) or not 0 < args.speed <= 1000:
            parser.error("--speed 必须大于 0 且不超过 1000 km/h。")
        if args.pick_only and args.file:
            parser.error("--pick-only 只用于在地图上绘制路线，不能同时给出路线文件。")
        if args._hold_session and not args.file:
            parser.error("后台运动轨迹会话需要路线文件。")
        # Load before any device work so a malformed file fails immediately
        # instead of after a tunnel has been negotiated.
        try:
            args.route = load_route(args.file, loop=args.loop) if args.file else None
        except ValueError as exc:
            parser.error(str(exc))

    if args._hold_session and args.command not in ("set", "route"):
        parser.error("_hold-session 只能用于 set 或 route 命令。")

    return args


def display_width(text):
    """Terminal cells needed for text: East Asian wide/fullwidth characters take two."""
    width = 0
    for char in str(text):
        if unicodedata.combining(char):
            continue
        width += 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
    return width


def pad_display(text, width):
    """Left-align text in a column of `width` terminal cells (str.ljust counts code points)."""
    text = str(text)
    return text + " " * max(0, width - display_width(text))


def format_table_row(cells, widths):
    """Join cells into one row; widths apply to every column except the last."""
    head = [pad_display(cell, width) for cell, width in zip(cells[:-1], widths)]
    return "  " + " ".join(head + [str(cells[-1])])


def describe_session_state(state):
    """Human-readable session status for one device's state file."""
    if not state:
        return "—"
    status = state.get("status")
    if status == "ready":
        lat = state.get("lat", "?")
        lon = state.get("lon", "?")
        pid = state.get("pid")
        if not is_process_alive(pid):
            return f"stale (进程 {pid} 已退出，定位可能仍在生效，请执行 clear)"
        if state.get("mode") == "route":
            if state.get("route_phase") == "completed":
                return f"已到终点，保持定位 ({lat}, {lon})"
            moving = (
                f"移动中 {state.get('progress', 0):.0%} "
                f"{state.get('speed_kmh', 0):g} km/h ({lat}, {lon})"
            )
            if state.get("loop"):
                moving += f" 第 {state.get('lap', 1)} 圈"
            return moving
        return f"ready ({lat}, {lon})"
    if status == "starting":
        return "starting"
    if status == "error":
        return "error"
    return "—"


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

    rows = []
    for udid in all_udids:
        alias = reverse_alias(udid, devices_data) or "—"
        is_default = "✓" if udid == default_udid else "—"
        status_str = describe_session_state(read_state(state_path_for(udid)))
        rows.append((udid, alias, is_default, status_str))

    widths = [
        max([40] + [display_width(row[0]) for row in rows]),
        max([12] + [display_width(row[1]) for row in rows]),
        6,
    ]
    print(format_table_row(("UDID", "别名", "默认", "状态"), widths))
    print(format_table_row(tuple("─" * w for w in widths) + ("─" * 20,), widths))
    for row in rows:
        print(format_table_row(row, widths))


def cmd_device_add(alias, udid, pmd3_bin, log_path=None):
    devices_data = read_devices()
    if looks_like_udid(alias):
        log_message(f"[!] 别名不能是 UDID 形式: {alias}")
        sys.exit(1)
    if udid and not looks_like_udid(udid):
        log_message(f"[!] UDID 格式不正确: {udid}")
        sys.exit(1)
    if not udid:
        discovered = discover_devices(pmd3_bin, log_path)
        if not discovered:
            log_message("[!] 未发现任何设备。请连接设备后重试。")
            sys.exit(1)
        if len(discovered) == 1:
            udid = discovered[0]
        else:
            udid = interactive_device_select(discovered, devices_data, log_path)
    previous = devices_data["aliases"].get(alias)
    devices_data["aliases"][alias] = udid
    write_devices(devices_data)
    if previous and previous != udid:
        log_message(f"[*] 别名 {alias} 原先指向 {previous}，已更新。")
    log_message(f"[+] 已注册别名: {alias} → {udid}")


def cmd_device_remove(alias):
    devices_data = read_devices()
    if alias not in devices_data["aliases"]:
        log_message(f"[!] 别名不存在: {alias}")
        sys.exit(1)
    del devices_data["aliases"][alias]
    if devices_data.get("default") == alias:
        devices_data["default"] = None
        log_message(f"[*] 默认设备已清除（之前指向已删除的别名 {alias}）。")
    write_devices(devices_data)
    log_message(f"[+] 已删除别名: {alias}")


def cmd_device_default(name=None):
    devices_data = read_devices()
    if name is None:
        default = devices_data.get("default")
        if default:
            udid = resolve_alias(default, devices_data)
            alias = reverse_alias(udid, devices_data)
            if alias:
                log_message(f"[*] 当前默认设备: {alias} ({udid})")
            else:
                log_message(f"[*] 当前默认设备: {udid}")
        else:
            log_message("[*] 未设置默认设备。")
        return
    udid = resolve_alias(name, devices_data)
    if udid == name and not looks_like_udid(name):
        known = ", ".join(sorted(devices_data["aliases"])) or "无"
        log_message(f"[!] 未知的设备别名: {name}（已注册别名: {known}）。")
        log_message("    请先执行 simlocation device add 注册别名，或直接传入完整 UDID。")
        sys.exit(1)
    devices_data["default"] = name
    write_devices(devices_data)
    log_message(f"[+] 默认设备已设置为: {name}" + (f" ({udid})" if name != udid else ""))


def cmd_status(pmd3_bin, log_path=None):
    cmd_device_list(pmd3_bin, log_path)


def doctor_check(label, status, detail):
    return {"label": label, "status": status, "detail": detail}


def collect_doctor_checks(pmd3_bin, device_flag=None):
    checks = [
        doctor_check(
            "Python",
            "ok",
            f"{sys.executable} ({sys.version.split()[0]})",
        )
    ]

    try:
        module_version = importlib_metadata.version("pymobiledevice3")
        checks.append(
            doctor_check("pymobiledevice3 模块", "ok", module_version)
        )
    except importlib_metadata.PackageNotFoundError:
        checks.append(
            doctor_check("pymobiledevice3 模块", "error", "当前 Python 未安装")
        )

    if not pmd3_bin:
        checks.append(
            doctor_check(
                "pymobiledevice3 CLI",
                "error",
                "未找到可执行文件（已检查 SIMLOCATION_PMD3、当前 Python 环境和 PATH）",
            )
        )
    else:
        try:
            result = subprocess.run(
                [pmd3_bin, "version"],
                capture_output=True,
                text=True,
                timeout=CMD_TIMEOUT_SECONDS,
            )
            cli_version = result.stdout.strip()
            if result.returncode == 0 and cli_version:
                checks.append(
                    doctor_check(
                        "pymobiledevice3 CLI",
                        "ok",
                        f"{pmd3_bin} ({cli_version})",
                    )
                )
            else:
                detail = result.stderr.strip() or f"退出码 {result.returncode}"
                checks.append(doctor_check("pymobiledevice3 CLI", "error", detail))
        except Exception as exc:
            checks.append(doctor_check("pymobiledevice3 CLI", "error", str(exc)))

    try:
        snapshot = get_tunneld_snapshot()
    except Exception as exc:
        checks.append(doctor_check("tunneld", "error", str(exc)))
        return checks

    if not isinstance(snapshot, dict):
        checks.append(doctor_check("tunneld", "error", "返回内容不是设备字典"))
        return checks
    checks.append(
        doctor_check(
            "tunneld",
            "ok",
            f"{TUNNELD_URL}，发现 {len(snapshot)} 台设备",
        )
    )

    devices_data = read_devices()
    default = devices_data.get("default")
    env_udid = os.environ.get("SIMLOCATION_UDID", "").strip() or None
    requested = device_flag or env_udid
    if requested:
        udid = resolve_alias(requested, devices_data)
        if udid == requested and not looks_like_udid(requested):
            known = ", ".join(sorted(devices_data["aliases"])) or "无"
            checks.append(
                doctor_check(
                    "目标设备",
                    "error",
                    f"未知的设备别名: {requested}（已注册别名: {known}）",
                )
            )
            return checks
        source = "--device" if device_flag else "SIMLOCATION_UDID"
        target_status = "ok"
        target_detail = f"{requested} ({udid})" if requested != udid else udid
        target_detail = f"{target_detail}，来自 {source}"
    elif default:
        udid = resolve_alias(default, devices_data)
        target_status = "ok"
        target_detail = f"{default} ({udid})" if default != udid else udid
    elif len(snapshot) == 1:
        udid = next(iter(snapshot))
        target_status = "warn"
        target_detail = f"未设置默认设备，将自动使用 {udid}"
    else:
        checks.append(
            doctor_check("目标设备", "error", "未设置默认设备且无法唯一选择")
        )
        return checks
    checks.append(doctor_check("目标设备", target_status, target_detail))

    if udid not in snapshot:
        checks.append(
            doctor_check("RSD", "error", "tunneld 当前未发现目标设备")
        )
        return checks

    rsd_pairs = extract_rsd_pairs(snapshot[udid])
    if not rsd_pairs:
        checks.append(doctor_check("RSD", "error", "未找到地址和端口"))
        return checks
    reachable = []
    unreachable = []
    for address, port in rsd_pairs:
        target = f"{address}:{port}"
        probe_ok = is_rsd_reachable(address, port, quiet=True)
        (reachable if probe_ok else unreachable).append(target)
    if reachable:
        detail = f"可达: {', '.join(reachable)}"
        if unreachable:
            detail += f"；不可达: {', '.join(unreachable)}"
        checks.append(doctor_check("RSD", "ok", detail))
    else:
        checks.append(
            doctor_check(
                "RSD",
                "warn",
                f"全部不可达: {', '.join(unreachable)}；set/clear 会先让 tunneld 取消它们再重建",
            )
        )

    state = read_state(state_path_for(udid))
    if not state:
        checks.append(doctor_check("后台会话", "ok", "当前没有状态记录"))
    elif state.get("status") == "ready":
        pid = state.get("pid")
        if isinstance(pid, int) and is_process_alive(pid):
            checks.append(doctor_check("后台会话", "ok", f"ready，PID {pid}"))
        else:
            checks.append(
                doctor_check("后台会话", "warn", "状态为 ready，但 PID 已失效")
            )
    elif state.get("status") == "error":
        checks.append(
            doctor_check(
                "后台会话",
                "warn",
                state.get("error", "上一次会话失败"),
            )
        )
    else:
        checks.append(
            doctor_check("后台会话", "ok", f"状态: {state.get('status', 'unknown')}")
        )

    return checks


def doctor_exit_code(checks):
    return 1 if any(check["status"] == "error" for check in checks) else 0


def cmd_doctor(pmd3_bin, device_flag=None):
    checks = collect_doctor_checks(pmd3_bin, device_flag=device_flag)
    markers = {"ok": "[+]", "warn": "[!]", "error": "[-]"}
    for check in checks:
        print(f"{markers[check['status']]} {check['label']}: {check['detail']}")
    return doctor_exit_code(checks)


def cmd_clear_all(pmd3_bin, connection_mode="auto", log_path=None):
    state_files = sorted(RUNTIME_DIR.glob("*.state.json"))
    active = []
    for sf in state_files:
        udid = sf.name.removesuffix(".state.json")
        if not looks_like_udid(udid):
            # e.g. a pre-3.0 single-device simlocation.state.json left in var/
            continue
        state = read_state(sf)
        if state and state.get("status") == "ready":
            active.append(udid)

    if not active:
        print("[*] 没有活跃的定位会话。")
        return

    for udid in active:
        print(f"[*] 正在清除设备 {udid} 的虚拟定位...")
        clear_location(pmd3_bin, connection_mode, log_path, udid=udid)


if __name__ == "__main__":
    args = parse_args()

    pmd3_bin = resolve_pymobiledevice3(required=args.command not in ("doctor", "web"))
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
        if not args.pid_file or not args.state_file:
            print("[-] --_hold-session 仅供内部使用，需要同时提供 --pid-file 与 --state-file。")
            sys.exit(2)
        pid_path = Path(args.pid_file)
        state_path = Path(args.state_file)
        route = getattr(args, "route", None)
        # A route session starts parked on the first waypoint; play_route takes
        # over once the DVT channel is up.
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
            speed_kmh=getattr(args, "speed", DEFAULT_ROUTE_SPEED_KMH),
            loop_route=getattr(args, "loop", False),
        )
        sys.exit(0)

    device_flag = getattr(args, "device", None)

    if args.command == "web":
        from simlocation_web import run_web_console

        run_web_console(sys.modules[__name__], args, pmd3_bin)
    elif args.command == "status":
        cmd_status(pmd3_bin, log_path)
    elif args.command == "doctor":
        doctor_result = cmd_doctor(pmd3_bin, device_flag=device_flag)
        if doctor_result:
            sys.exit(doctor_result)
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
        listen_host, open_browser = map_bind_from_args(args)
        coords = run_map_picker(
            amap_key,
            listen_host=listen_host,
            listen_port=getattr(args, "port", None),
            open_browser=open_browser,
        )
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
    elif args.command == "route":
        route = args.route
        if route is None:
            amap_key = os.environ.get("SIMLOCATION_AMAP_KEY", "").strip() or None
            if not amap_key:
                print(AMAP_KEY_HINT)
            listen_host, open_browser = map_bind_from_args(args)
            route = run_map_picker(
                amap_key,
                listen_host=listen_host,
                listen_port=getattr(args, "port", None),
                open_browser=open_browser,
                route_mode=True,
                speed_kmh=args.speed,
                loop_route=args.loop,
                pick_only=args.pick_only,
            )
            if route is None:
                print("[-] 未提交路线（超时或关闭了浏览器）。")
                sys.exit(1)
        if args.pick_only:
            print(json.dumps({"points": route.waypoints()}, ensure_ascii=False))
        else:
            auto_set_route(
                route,
                args.speed,
                args.loop,
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
