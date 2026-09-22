"""Persistent browser console over the existing CLI operations (stdlib HTTP only)."""

import copy
import json
import math
import os
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit


class BusyError(ValueError):
    pass


class WebConsole:
    """One serialized operation at a time; status reads never wait for device I/O."""

    def __init__(self, cli, pmd3_bin, args):
        self.cli = cli
        self.pmd3_bin = pmd3_bin
        self.args = args
        self.lock = threading.RLock()
        self.job = None
        self.worker = None
        self.discovered = []
        self.scanned_at = None
        self.sequence = 0

    def target(self, value):
        if not isinstance(value, str) or not value.strip():
            raise ValueError("请先选择设备或输入完整 UDID。")
        udid = self.cli.resolve_alias(value.strip(), self.cli.read_devices())
        if not self.cli.looks_like_udid(udid):
            raise ValueError("未知设备别名或无效 UDID。")
        return udid

    def state(self):
        cli = self.cli
        config = cli.read_devices()
        default = config.get("default")
        default_udid = cli.resolve_alias(default, config) if default else None
        requested = self.args.device or os.environ.get("SIMLOCATION_UDID") or default
        preferred = cli.resolve_alias(requested, config) if requested else None
        with self.lock:
            discovered = list(self.discovered)
            job = copy.deepcopy(self.job)
            scanned_at = self.scanned_at
        known = set(discovered) | set(config["aliases"].values())
        known.update(path.name.removesuffix(".state.json")
                     for path in cli.RUNTIME_DIR.glob("*.state.json"))
        known.update(value for value in (preferred, default_udid) if value)
        devices = []
        for udid in sorted(known):
            if not isinstance(udid, str) or not cli.looks_like_udid(udid):
                continue
            state = cli.read_state(cli.state_path_for(udid)) or {}
            devices.append({
                "udid": udid,
                "aliases": [name for name, value in config["aliases"].items() if value == udid],
                "default": udid == default_udid,
                "discovered": udid in discovered,
                "state": state,
                "description": cli.describe_session_state(state),
            })
        return {
            "devices": devices, "default": default_udid, "preferred": preferred,
            "job": job, "scanned_at": scanned_at,
            "version": cli.read_project_version(),
            "connection": self.args.connection, "debug": self.args.debug,
            "provider": "amap" if os.environ.get("SIMLOCATION_AMAP_KEY", "").strip() else "osm",
            "max_route_bytes": cli.MAX_ROUTE_BYTES, "max_route_points": cli.MAX_ROUTE_POINTS,
        }

    def prepare(self, data):
        """Validate the complete request before a worker can touch a device."""
        cli = self.cli
        action = data.get("action")
        allowed = {"discover", "doctor", "set", "route", "clear", "clear_all",
                   "device_add", "device_remove", "device_default"}
        if not isinstance(action, str) or action not in allowed:
            raise ValueError("不支持的操作。")
        connection = data.get("connection", self.args.connection)
        if connection not in ("auto", "rsd"):
            raise ValueError("连接模式只能为 auto 或 rsd。")
        debug = data.get("debug", self.args.debug)
        if not isinstance(debug, bool):
            raise ValueError("debug 必须为布尔值。")
        log_path = Path(self.args.log_file) if debug else None
        device = data.get("device")
        udid = None
        if action in {"set", "route", "clear", "device_add", "device_default"} or (
            action == "doctor" and device
        ):
            udid = self.target(device)
        options = {"connection_mode": connection, "log_path": log_path, "udid": udid}
        if action == "set":
            lat, lon = cli.validate_coordinates(data.get("lat"), data.get("lon"))
            callback = lambda: cli.auto_set_location(lat, lon, self.pmd3_bin, **options)
        elif action == "route":
            speed = data.get("speed", cli.DEFAULT_ROUTE_SPEED_KMH)
            if (
                isinstance(speed, bool) or not isinstance(speed, (float, int))
                or not math.isfinite(speed) or not 0 < speed <= 1000
            ):
                raise ValueError("速度必须大于 0 且不超过 1000 km/h。")
            loop = data.get("loop", False)
            if not isinstance(loop, bool):
                raise ValueError("loop 必须为布尔值。")
            speed_noise = data.get("speed_noise", 0.0)
            position_noise = data.get("position_noise", 0.0)
            cli.validate_route_noise(speed_noise, position_noise)
            route = cli.Route(data.get("points"), loop=loop)
            callback = lambda: cli.auto_set_route(
                route, speed, loop, self.pmd3_bin, **options,
                speed_noise=speed_noise, position_noise=position_noise,
            )
        elif action == "clear":
            callback = lambda: cli.clear_location(self.pmd3_bin, **options)
        elif action == "clear_all":
            if data.get("confirm") is not True:
                raise ValueError("请确认清除所有活跃会话。")
            callback = lambda: self.clear_all(connection, log_path)
        elif action in {"device_add", "device_remove"}:
            alias = data.get("alias")
            if (
                not isinstance(alias, str) or not alias.strip() or len(alias) > 100
                or any(ord(c) < 32 for c in alias)
            ):
                raise ValueError("别名需为 1–100 个可显示字符。")
            alias = alias.strip()
            if cli.looks_like_udid(alias):
                raise ValueError("别名不能为 UDID。")
            if action == "device_add":
                previous = cli.read_devices()["aliases"].get(alias)
                if previous and previous != udid:
                    raise ValueError("该别名已属于其他设备，请先删除或换一个名称。")
                callback = lambda: cli.cmd_device_add(alias, udid, self.pmd3_bin, log_path)
            else:
                if alias not in cli.read_devices()["aliases"]:
                    raise ValueError("别名不存在。")
                callback = lambda: cli.cmd_device_remove(alias)
        elif action == "device_default":
            callback = lambda: cli.cmd_device_default(udid)
        elif action == "doctor":
            callback = lambda: {"checks": cli.collect_doctor_checks(self.pmd3_bin, device_flag=udid)}
        else:
            callback = self.discover
        return action, udid, log_path, callback

    def discover(self):
        discovered = self.cli.discover_devices(self.pmd3_bin)
        with self.lock:
            self.discovered = discovered
            self.scanned_at = time.time()
        return {"count": len(discovered)}

    def clear_all(self, connection, log_path):
        # Continue through failures so a disconnected phone cannot strand others.
        failed = []
        for path in sorted(self.cli.RUNTIME_DIR.glob("*.state.json")):
            udid = path.name.removesuffix(".state.json")
            state = self.cli.read_state(path) or {}
            if not self.cli.looks_like_udid(udid) or state.get("status") != "ready":
                continue
            try:
                self.cli.clear_location(self.pmd3_bin, connection, log_path, udid=udid)
            except (Exception, SystemExit):
                failed.append(udid)
        if failed:
            raise ValueError("部分设备清除失败，请重试：" + ", ".join(failed))

    def submit(self, data):
        with self.lock:
            if self.job and self.job["status"] == "running":
                raise BusyError("已有操作正在执行，请等待结果。")
            action, udid, log_path, callback = self.prepare(data)
            self.sequence += 1
            self.job = {"id": self.sequence, "action": action, "device": udid,
                        "status": "running", "messages": [], "result": None}
            self.worker = threading.Thread(target=self.execute, args=(callback, log_path))
            self.worker.start()
            return copy.deepcopy(self.job)

    def record(self, message):
        with self.lock:
            self.job["messages"].append(str(message))
            self.job["messages"] = self.job["messages"][-100:]

    def execute(self, callback, log_path):
        self.cli.LOG_CONTEXT.sink = self.record
        result, status = None, "succeeded"
        try:
            if log_path:
                log_path.parent.mkdir(parents=True, exist_ok=True)
            result = callback()
        except (Exception, SystemExit) as exc:
            status = "failed"
            self.record(str(exc) if not isinstance(exc, SystemExit) else "操作失败，请查看上方日志或运行诊断。")
        finally:
            self.cli.LOG_CONTEXT.sink = None
            with self.lock:
                self.job.update(status=status, result=result)

    def close(self):
        if self.worker:
            self.worker.join()


class ConsoleHandler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def log_message(self, *_args):
        # URLs contain the credential. Do not write access logs.
        pass

    def respond(self, status, data, content_type="application/json; charset=utf-8"):
        if not isinstance(data, bytes):
            data = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass  # Navigating away must not cancel a device operation.

    def route(self):
        parts = urlsplit(self.path)
        query = parse_qs(parts.query)
        supplied = self.headers.get("X-SimLocation-Token", "")
        if not parts.path.startswith("/api/"):
            supplied = (query.get("t") or [supplied])[0]
        if not secrets.compare_digest(supplied.encode(), self.server.access_token.encode()):
            self.respond(403, {"error": "访问令牌无效，请使用终端打印的完整地址。"})
            return None, query
        origin = self.headers.get("Origin")
        if origin and origin != "http://" + self.headers.get("Host", ""):
            self.respond(403, {"error": "不接受跨站请求。"})
            return None, query
        return parts.path, query

    def do_GET(self):
        if urlsplit(self.path).path == "/favicon.ico":
            # Browsers ask for this on every load and never carry the token, so
            # the console would log a 403 against itself. 204 says "nothing
            # here" without revealing more than the 403 already did.
            self.respond(204, b"")
            return
        path, query = self.route()
        if path is None:
            return
        app = self.server.app
        if path == "/api/state":
            self.respond(200, app.state())
        elif path == "/picker":
            html = app.cli.render_map_html(
                os.environ.get("SIMLOCATION_AMAP_KEY", "").strip() or None,
                route_mode=(query.get("mode") == ["route"]), pick_only=True,
            )
            self.respond(200, html, "text/html; charset=utf-8")
        elif path in ("/", "/console.js", "/console.css"):
            filename, mime = {
                "/": ("console.html", "text/html; charset=utf-8"),
                "/console.js": ("console.js", "text/javascript; charset=utf-8"),
                "/console.css": ("console.css", "text/css; charset=utf-8"),
            }[path]
            data = (app.cli.PROJECT_DIR / "web" / filename).read_bytes()
            if path == "/":
                data = data.replace(b"{{TOKEN_QUERY}}", urlencode({"t": self.server.access_token}).encode())
            self.respond(200, data, mime)
        else:
            self.respond(404, {"error": "接口不存在。"})

    def do_POST(self):
        path, _query = self.route()
        if path is None:
            return
        app = self.server.app
        if path not in ("/api/action", "/api/route"):
            self.respond(404, {"error": "接口不存在。"})
            return
        try:
            if self.headers.get_content_type() != "application/json" or self.headers.get("Transfer-Encoding"):
                raise ValueError("请求需为 JSON。")
            length = int(self.headers.get("Content-Length", 0))
            limit = app.cli.MAX_ROUTE_BYTES * 2 + 8192
            if length > limit:
                self.respond(413, {"error": "请求内容过大。"})
                return
            if length <= 0:
                raise ValueError("请求内容为空。")
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict):
                raise ValueError("请求需为 JSON 对象。")
            if path == "/api/route":
                content = data.get("content")
                if not isinstance(content, str):
                    raise ValueError("缺少路线文件内容。")
                route = app.cli.parse_route_data(content.encode("utf-8"), data.get("format", "json"))
                self.respond(200, {"points": route.waypoints(), "distance_m": route.total_m})
            else:
                self.respond(202, app.submit(data))
        except BusyError as exc:
            self.respond(409, {"error": str(exc)})
        except (ValueError, TypeError, RecursionError) as exc:
            self.respond(400, {"error": str(exc)})
        except OSError as exc:
            self.respond(500, {"error": str(exc)})


def run_web_console(cli, args, pmd3_bin):
    listen, open_browser = cli.map_bind_from_args(args)
    listen = cli.resolve_map_listen_host(listen)
    try:
        port = args.port if args.port is not None else int(os.environ.get("SIMLOCATION_MAP_PORT") or 0)
        if not 0 <= port <= 65535:
            raise ValueError("端口需在 0–65535 之间。")
        server = ThreadingHTTPServer((listen, port), ConsoleHandler)
    except (OSError, ValueError) as exc:
        cli.log_message(f"[!] 无法启动网页控制台: {exc}")
        raise SystemExit(1) from exc
    server.app = WebConsole(cli, pmd3_bin, args)
    # Persistent controls require authentication on loopback as well as LAN.
    server.access_token = os.environ.get("SIMLOCATION_MAP_TOKEN", "").strip() or secrets.token_urlsafe(24)
    port = server.server_address[1]
    hosts = (cli.guess_reachable_hosts() or [listen]) if listen == "0.0.0.0" else [listen]
    urls = [f"http://{host}:{port}/?{urlencode({'t': server.access_token})}" for host in hosts]
    cli.log_message("[*] 网页控制台已启动。Ctrl+C 停止服务；后台定位会话会继续保持，结束定位请先清除。")
    for url in urls:
        print(f"    {url}", flush=True)
    server.app.submit({"action": "discover"})
    if open_browser and cli.is_loopback_host(listen):
        cli._open_app_window(urls[0])
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        cli.log_message("[*] 正在关闭网页服务，等待当前操作完成。")
    finally:
        server.server_close()
        server.app.close()
