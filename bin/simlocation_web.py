"""Persistent browser console over the existing CLI operations (stdlib HTTP only).

Access is a login page plus an HttpOnly session cookie, so no long-lived secret
ever travels in a URL. With SIMLOCATION_WEB_PASSWORD_HASH set, the page asks
for a username and password a password manager can fill; without it, startup
prints a single-use login link instead.
"""

import base64
import copy
import getpass
import hashlib
import hmac
import html
import ipaddress
import json
import math
import os
import re
import secrets
import sys
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

DEFAULT_WEB_USER = "simlocation"
SESSION_COOKIE = "simloc_session"
CSRF_HEADER = "X-SimLocation-CSRF"
SESSION_IDLE_SECONDS = 7 * 24 * 3600
SESSION_MAX_SECONDS = 30 * 24 * 3600
MAX_SESSIONS = 16
LOGIN_CODE_SECONDS = 600
LOGIN_WINDOW_SECONDS = 900
LOGIN_FAILURES_PER_CLIENT = 5
LOGIN_FAILURES_TOTAL = 50
LOGIN_FORM_MAX_BYTES = 4096
PASSWORD_MIN_CHARS = 12
PASSWORD_MAX_CHARS = 1024
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2 ** 14, 8, 1
SCRYPT_MAXMEM = 64 * 1024 * 1024
FAVORITES_MAX = 200
FAVORITE_NAME_MAX = 60
TUNNEL_STATUS_TTL_SECONDS = 3
JSON_TYPE = "application/json; charset=utf-8"
HTML_TYPE = "text/html; charset=utf-8"
# The console and login pages load nothing from other origins. The map picker
# is a separate document in an iframe and keeps its own CDN and tile hosts.
PAGE_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; frame-src 'self'; frame-ancestors 'self'; form-action 'self'; "
    "base-uri 'none'; object-src 'none'"
)


class BusyError(ValueError):
    pass


class LoginThrottled(Exception):
    def __init__(self, retry_after):
        super().__init__(retry_after)
        self.retry_after = max(1.0, retry_after)


def _b64(raw):
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def hash_password(password, salt=None):
    """Encode as scrypt:N:r:p:salt:key. No "$", so systemd env files keep it verbatim."""
    if not isinstance(password, str) or not password:
        raise ValueError("密码不能为空。")
    salt = secrets.token_bytes(16) if salt is None else salt
    key = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=SCRYPT_N, r=SCRYPT_R,
                         p=SCRYPT_P, maxmem=SCRYPT_MAXMEM, dklen=32)
    return f"scrypt:{SCRYPT_N}:{SCRYPT_R}:{SCRYPT_P}:{_b64(salt)}:{_b64(key)}"


def parse_password_hash(encoded):
    """Validate at startup, so a mangled env file fails loudly instead of at login."""
    try:
        scheme, n, r, p, salt, key = encoded.strip().split(":")
        n, r, p = int(n), int(r), int(p)
        salt, key = _unb64(salt), _unb64(key)
    except (AttributeError, ValueError):
        raise ValueError("SIMLOCATION_WEB_PASSWORD_HASH 格式无效，请用 `simlocation web --hash-password` 重新生成。") from None
    # Bounded parameters keep a tampered hash from turning each login into a
    # gigabyte allocation.
    if (scheme != "scrypt" or n not in (2 ** 14, 2 ** 15, 2 ** 16) or r != 8 or p != 1
            or len(salt) < 16 or len(key) < 32):
        raise ValueError("SIMLOCATION_WEB_PASSWORD_HASH 参数不受支持，请用 `simlocation web --hash-password` 重新生成。")
    return n, r, p, salt, key


def verify_password(password, parsed):
    n, r, p, salt, key = parsed
    candidate = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p,
                               maxmem=SCRYPT_MAXMEM, dklen=len(key))
    return hmac.compare_digest(candidate, key)


class Auth:
    """In-memory sessions: restarting the service signs every browser out."""

    def __init__(self, password_hash=None, username=DEFAULT_WEB_USER, clock=time.monotonic):
        self.password = parse_password_hash(password_hash) if password_hash else None
        self.username = username
        self.clock = clock
        self.lock = threading.Lock()
        # One scrypt at a time bounds memory and closes the race where parallel
        # guesses all pass the throttle check before any failure is recorded.
        self.verify_lock = threading.Lock()
        self.sessions = {}
        self.codes = {}
        self.failures = {}

    @property
    def uses_password(self):
        return self.password is not None

    def _expired(self, session, now):
        return now - session["seen"] > SESSION_IDLE_SECONDS or now - session["created"] > SESSION_MAX_SECONDS

    def _prune(self, now):
        for sid in [sid for sid, session in self.sessions.items() if self._expired(session, now)]:
            del self.sessions[sid]
        for code in [code for code, expiry in self.codes.items() if expiry <= now]:
            del self.codes[code]

    def _new_session(self, now):
        self._prune(now)
        while len(self.sessions) >= MAX_SESSIONS:
            del self.sessions[min(self.sessions, key=lambda sid: self.sessions[sid]["seen"])]
        sid = secrets.token_urlsafe(32)
        self.sessions[sid] = {"csrf": secrets.token_urlsafe(32), "created": now, "seen": now}
        return sid

    def issue_code(self):
        with self.lock:
            code = secrets.token_urlsafe(24)
            self.codes[code] = self.clock() + LOGIN_CODE_SECONDS
            return code

    def redeem_code(self, code):
        with self.lock:
            now = self.clock()
            self._prune(now)
            # pop() keeps the link single-use even when two requests race.
            if not isinstance(code, str) or self.codes.pop(code, None) is None:
                return None
            return self._new_session(now)

    def _check_throttle(self, client, now):
        cutoff = now - LOGIN_WINDOW_SECONDS
        for key in list(self.failures):
            entries = self.failures[key]
            while entries and entries[0] <= cutoff:
                entries.popleft()
            if not entries:
                del self.failures[key]
        mine = self.failures.get(client)
        if mine and len(mine) >= LOGIN_FAILURES_PER_CLIENT:
            raise LoginThrottled(mine[0] + LOGIN_WINDOW_SECONDS - now)
        if sum(len(entries) for entries in self.failures.values()) >= LOGIN_FAILURES_TOTAL:
            oldest = min(entries[0] for entries in self.failures.values())
            raise LoginThrottled(oldest + LOGIN_WINDOW_SECONDS - now)

    def login(self, client, username, password):
        """New session id, or None for wrong credentials. Raises LoginThrottled."""
        if self.password is None:
            return None
        with self.verify_lock:
            with self.lock:
                self._check_throttle(client, self.clock())
            ok = verify_password(password, self.password)
            ok = hmac.compare_digest(username.encode("utf-8"), self.username.encode("utf-8")) and ok
            with self.lock:
                now = self.clock()
                if not ok:
                    self.failures.setdefault(client, deque()).append(now)
                    return None
                self.failures.pop(client, None)
                return self._new_session(now)

    def session(self, sid):
        if not sid:
            return None
        with self.lock:
            now = self.clock()
            session = self.sessions.get(sid)
            if session is None:
                return None
            if self._expired(session, now):
                del self.sessions[sid]
                return None
            session["seen"] = now
            return dict(session)

    def logout(self, sid):
        with self.lock:
            self.sessions.pop(sid, None)


def clean_favorite_name(value):
    if not isinstance(value, str):
        raise ValueError("请填写收藏名称。")
    name = " ".join(value.split())
    if not name:
        raise ValueError("请填写收藏名称。")
    if len(name) > FAVORITE_NAME_MAX:
        raise ValueError(f"名称最多 {FAVORITE_NAME_MAX} 个字符。")
    if any(ord(c) < 32 or ord(c) == 127 for c in name):
        raise ValueError("名称包含不可显示的字符。")
    return name


class WebConsole:
    """One serialized operation at a time; status reads never wait for device I/O."""

    def __init__(self, cli, pmd3_bin, args):
        self.cli = cli
        self.pmd3_bin = pmd3_bin
        self.args = args
        self.lock = threading.RLock()
        self.favorites_lock = threading.Lock()
        self.tunnel_lock = threading.Lock()
        self.tunnel_cache = None
        self.job = None
        self.worker = None
        self.discovered = []
        self.scanned_at = None
        self.sequence = 0
        self.label = " ".join(os.environ.get("SIMLOCATION_WEB_LABEL", "").split())[:40] or None

    def target(self, value):
        if not isinstance(value, str) or not value.strip():
            raise ValueError("请先选择设备或输入完整 UDID。")
        udid = self.cli.resolve_alias(value.strip(), self.cli.read_devices())
        if not self.cli.looks_like_udid(udid):
            raise ValueError("未知设备别名或无效 UDID。")
        return udid

    def default_point(self):
        coords = self.cli.env_default_coordinates()
        if not coords:
            return None
        try:
            lat, lon = self.cli.validate_coordinates(*coords)
        except ValueError:
            return None
        return {"lat": lat, "lon": lon}

    def tunnel_status(self):
        """UDIDs tunneld holds a tunnel for, or None when tunneld does not answer."""
        with self.tunnel_lock:
            now = time.monotonic()
            if self.tunnel_cache is None or now - self.tunnel_cache[0] >= TUNNEL_STATUS_TTL_SECONDS:
                try:
                    response = self.cli.requests.get(self.cli.TUNNELD_URL, timeout=1)
                    response.raise_for_status()
                    data = response.json()
                    value = sorted(
                        udid for udid, records in data.items()
                        if self.cli.looks_like_udid(udid) and self.cli.extract_rsd_pairs(records)
                    ) if isinstance(data, dict) else None
                except Exception:
                    value = None
                self.tunnel_cache = (now, value)
            return self.tunnel_cache[1]

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
            "version": cli.read_project_version(), "label": self.label,
            "connection": self.args.connection, "debug": self.args.debug,
            "provider": "amap" if os.environ.get("SIMLOCATION_AMAP_KEY", "").strip() else "osm",
            "max_route_bytes": cli.MAX_ROUTE_BYTES, "max_route_points": cli.MAX_ROUTE_POINTS,
            "defaults": self.default_point(), "tunnels": self.tunnel_status(),
        }

    def favorites_path(self):
        return self.cli.RUNTIME_DIR / "favorites.json"

    def favorites(self):
        data = self.cli.read_state(self.favorites_path())
        items = data.get("items") if isinstance(data, dict) else None
        result = []
        for item in items if isinstance(items, list) else []:
            # A hand-edited or truncated file loses bad rows, not the whole list.
            try:
                if not isinstance(item, dict) or not re.fullmatch(r"[0-9a-f]{12}", str(item.get("id"))):
                    continue
                lat, lon = self.cli.validate_coordinates(item.get("lat"), item.get("lon"))
                result.append({"id": item["id"], "name": clean_favorite_name(item.get("name")),
                               "lat": lat, "lon": lon})
            except ValueError:
                continue
        return result

    def edit_favorites(self, data):
        operation = data.get("op")
        with self.favorites_lock:
            items = self.favorites()
            if operation == "add":
                name = clean_favorite_name(data.get("name"))
                lat, lon = self.cli.validate_coordinates(data.get("lat"), data.get("lon"))
                lat, lon = round(lat, 7), round(lon, 7)
                for item in items:
                    if abs(item["lat"] - lat) < 1e-6 and abs(item["lon"] - lon) < 1e-6:
                        raise ValueError(f"这个位置已收藏为「{item['name']}」。")
                if len(items) >= FAVORITES_MAX:
                    raise ValueError(f"收藏最多 {FAVORITES_MAX} 个，请先删除不用的。")
                items.insert(0, {"id": secrets.token_hex(6), "name": name, "lat": lat, "lon": lon})
            elif operation == "remove":
                kept = [item for item in items if item["id"] != data.get("id")]
                if len(kept) == len(items):
                    raise ValueError("收藏不存在，可能已被删除。")
                items = kept
            else:
                raise ValueError("不支持的收藏操作。")
            self.cli.write_state(self.favorites_path(), {"version": 1, "items": items})
        return {"items": items}

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


def cookie_value(header, name):
    for part in (header or "").split(";"):
        key, separator, value = part.strip().partition("=")
        if separator and key == name:
            return value
    return None


def session_cookie(value, max_age):
    # No Secure flag: the console is plain HTTP inside WireGuard or on
    # loopback, and a Secure cookie would never be sent back over it.
    return f"{SESSION_COOKIE}={value}; Path=/; Max-Age={max_age}; HttpOnly; SameSite=Strict"


class ConsoleHandler(BaseHTTPRequestHandler):
    server_version = "SimLocation"
    sys_version = ""

    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def log_message(self, *_args):
        # The console polls every two seconds; an access log would only be noise.
        pass

    def respond(self, status, data, content_type=JSON_TYPE, headers=(), referrer="no-referrer"):
        if not isinstance(data, bytes):
            data = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", referrer)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        for name, value in headers:
            self.send_header(name, value)
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass  # Navigating away must not cancel a device operation.

    def redirect(self, location, headers=()):
        self.respond(303, b"", "text/plain; charset=utf-8", [("Location", location), *headers])

    def parsed_path(self):
        try:
            parts = urlsplit(self.path)
        except ValueError:
            return None, {}
        return parts.path, parse_qs(parts.query)

    def host_allowed(self):
        """Refuse names other than IP literals: a DNS-rebinding page arrives by name."""
        try:
            hostname = urlsplit("//" + self.headers.get("Host", "")).hostname
        except ValueError:
            return False
        if not hostname:
            return False
        if hostname == "localhost" or hostname in getattr(self.server, "allowed_hosts", ()):
            return True
        try:
            ipaddress.ip_address(hostname)
        except ValueError:
            return False
        return True

    def cross_site(self):
        origin = self.headers.get("Origin")
        host = self.headers.get("Host", "")
        site = self.headers.get("Sec-Fetch-Site")
        if origin is not None and origin not in (f"http://{host}", f"https://{host}"):
            # A form POST under a no-referrer policy sends "Origin: null" even
            # from our own page. Sec-Fetch-Site cannot be set by page scripts,
            # and a sandboxed cross-site frame reports "cross-site" there.
            return not (origin == "null" and site == "same-origin")
        return (site or "same-origin") not in ("same-origin", "none")

    def current_session(self):
        sid = cookie_value(self.headers.get("Cookie"), SESSION_COOKIE)
        return sid, self.server.auth.session(sid)

    def csrf_ok(self, session):
        supplied = self.headers.get(CSRF_HEADER, "").encode("utf-8", "replace")
        return hmac.compare_digest(supplied, session["csrf"].encode("ascii"))

    def asset(self, name):
        return (self.server.app.cli.PROJECT_DIR / "web" / name).read_bytes()

    def render_login(self, message="", status=200, headers=()):
        auth = self.server.auth
        page = self.asset("login.html").decode("utf-8")
        for key, value in {
            "{{LABEL}}": html.escape(self.server.app.label or "网页控制台"),
            "{{USERNAME}}": html.escape(auth.username, quote=True),
            "{{MESSAGE}}": html.escape(message),
            "{{MESSAGE_HIDDEN}}": "" if message else " hidden",
            "{{FORM_HIDDEN}}": "" if auth.uses_password else " hidden",
            "{{LINK_HIDDEN}}": " hidden" if auth.uses_password else "",
        }.items():
            page = page.replace(key, value)
        # same-origin keeps a real Origin on the form POST; URLs hold no secrets.
        self.respond(status, page.encode("utf-8"), HTML_TYPE,
                     [("Content-Security-Policy", PAGE_CSP), *headers], referrer="same-origin")

    def login_page(self, query):
        auth = self.server.auth
        if not auth.uses_password and "code" in query:
            sid = auth.redeem_code(query["code"][0])
            if sid:
                self.redirect("/", [("Set-Cookie", session_cookie(sid, SESSION_MAX_SECONDS))])
                return
            self.render_login("登录链接无效或已用过：一次性链接只能用一次，10 分钟内有效。请重启服务获取新链接。", 401)
            return
        if self.current_session()[1] is not None:
            self.redirect("/")
            return
        self.render_login()

    def login_submit(self):
        auth = self.server.auth
        if not auth.uses_password:
            self.render_login("此控制台未设置密码，请使用启动时打印的一次性登录链接。", 400)
            return
        try:
            if (self.headers.get_content_type() != "application/x-www-form-urlencoded"
                    or self.headers.get("Transfer-Encoding")):
                raise ValueError
            length = int(self.headers.get("Content-Length", 0))
            if not 0 < length <= LOGIN_FORM_MAX_BYTES:
                raise ValueError
            form = parse_qs(self.rfile.read(length).decode("utf-8"),
                            keep_blank_values=True, max_num_fields=8)
        except (ValueError, UnicodeDecodeError):
            self.render_login("登录请求无效，请重试。", 400)
            return
        username = (form.get("username") or [""])[0]
        password = (form.get("password") or [""])[0]
        if len(username) > 200 or len(password) > PASSWORD_MAX_CHARS:
            self.render_login("用户名或密码过长。", 400)
            return
        try:
            sid = auth.login(self.client_address[0], username, password)
        except LoginThrottled as exc:
            minutes = math.ceil(exc.retry_after / 60)
            self.render_login(f"失败次数过多，请 {minutes} 分钟后再试。", 429,
                              [("Retry-After", str(math.ceil(exc.retry_after)))])
            return
        if sid is None:
            self.render_login("用户名或密码不正确。", 401)
            return
        self.redirect("/", [("Set-Cookie", session_cookie(sid, SESSION_MAX_SECONDS))])

    def do_GET(self):
        path, query = self.parsed_path()
        if path is None:
            self.respond(400, {"error": "请求地址无效。"})
            return
        if not self.host_allowed():
            self.respond(421, {"error": "请用 IP 地址访问控制台。"})
            return
        if path == "/favicon.ico":
            # Browsers request this unauthenticated on every load; 204 avoids a
            # console logging errors against itself.
            self.respond(204, b"")
            return
        if path == "/console.css":
            # Public: the login page needs it, and it is in the public repo anyway.
            self.respond(200, self.asset("console.css"), "text/css; charset=utf-8")
            return
        if path == "/login":
            self.login_page(query)
            return
        _sid, session = self.current_session()
        if session is None:
            if path == "/":
                self.redirect("/login")
            else:
                self.respond(401, {"error": "登录已失效，请重新登录。"})
            return
        if path.startswith("/api/"):
            if self.cross_site():
                self.respond(403, {"error": "不接受跨站请求。"})
                return
            if not self.csrf_ok(session):
                # A tab left open across a re-login holds a stale token.
                self.respond(401, {"error": "页面已过期，请刷新。"})
                return
        app = self.server.app
        if path == "/api/state":
            self.respond(200, app.state())
        elif path == "/api/favorites":
            self.respond(200, {"items": app.favorites()})
        elif path == "/picker":
            page = app.cli.render_map_html(
                os.environ.get("SIMLOCATION_AMAP_KEY", "").strip() or None,
                route_mode=(query.get("mode") == ["route"]), pick_only=True,
            )
            self.respond(200, page, HTML_TYPE)
        elif path == "/":
            page = self.asset("console.html").replace(b"{{CSRF_TOKEN}}", session["csrf"].encode("ascii"))
            self.respond(200, page, HTML_TYPE, [("Content-Security-Policy", PAGE_CSP)])
        elif path == "/console.js":
            self.respond(200, self.asset("console.js"), "text/javascript; charset=utf-8")
        else:
            self.respond(404, {"error": "接口不存在。"})

    def do_POST(self):
        path, _query = self.parsed_path()
        if path is None:
            self.respond(400, {"error": "请求地址无效。"})
            return
        if not self.host_allowed():
            self.respond(421, {"error": "请用 IP 地址访问控制台。"})
            return
        if self.cross_site():
            self.respond(403, {"error": "不接受跨站请求。"})
            return
        if path == "/login":
            self.login_submit()
            return
        sid, session = self.current_session()
        if session is None or not self.csrf_ok(session):
            self.respond(401, {"error": "登录已失效，请重新登录。"})
            return
        if path == "/logout":
            self.server.auth.logout(sid)
            self.respond(200, {"ok": True}, headers=[("Set-Cookie", session_cookie("", 0))])
            return
        app = self.server.app
        if path not in ("/api/action", "/api/route", "/api/favorites"):
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
            elif path == "/api/favorites":
                self.respond(200, app.edit_favorites(data))
            else:
                self.respond(202, app.submit(data))
        except BusyError as exc:
            self.respond(409, {"error": str(exc)})
        except (ValueError, TypeError, RecursionError) as exc:
            self.respond(400, {"error": str(exc)})
        except OSError as exc:
            self.respond(500, {"error": str(exc)})


def read_new_password(stream=None):
    """Prompt twice on a terminal; read one line when piped from a script."""
    stream = stream or sys.stdin
    if stream.isatty():
        first = getpass.getpass("新密码: ")
        if first != getpass.getpass("再输一次: "):
            raise ValueError("两次输入不一致。")
        return first
    return stream.readline().rstrip("\r\n")


def print_password_hash(cli, stream=None):
    try:
        password = read_new_password(stream)
        if len(password) < PASSWORD_MIN_CHARS:
            raise ValueError(f"密码至少 {PASSWORD_MIN_CHARS} 个字符，建议直接用密码管理器生成。")
        print("SIMLOCATION_WEB_PASSWORD_HASH=" + hash_password(password), flush=True)
    except (ValueError, EOFError, KeyboardInterrupt) as exc:
        cli.log_message(f"[!] {exc}")
        raise SystemExit(1) from None


def url_host(host):
    return f"[{host}]" if ":" in host else host


def run_web_console(cli, args, pmd3_bin):
    if getattr(args, "hash_password", False):
        print_password_hash(cli)
        return
    listen, open_browser = cli.map_bind_from_args(args)
    listen = cli.resolve_map_listen_host(listen)
    try:
        auth = Auth(os.environ.get("SIMLOCATION_WEB_PASSWORD_HASH", "").strip() or None,
                    os.environ.get("SIMLOCATION_WEB_USER", "").strip() or DEFAULT_WEB_USER)
        port = args.port if args.port is not None else int(os.environ.get("SIMLOCATION_MAP_PORT") or 0)
        if not 0 <= port <= 65535:
            raise ValueError("端口需在 0–65535 之间。")
        server = ThreadingHTTPServer((listen, port), ConsoleHandler)
    except (OSError, ValueError) as exc:
        cli.log_message(f"[!] 无法启动网页控制台: {exc}")
        raise SystemExit(1) from exc
    server.app = WebConsole(cli, pmd3_bin, args)
    server.auth = auth
    server.allowed_hosts = {
        name.strip().lower() for name in os.environ.get("SIMLOCATION_WEB_HOSTS", "").split(",") if name.strip()
    }
    port = server.server_address[1]
    hosts = (cli.guess_reachable_hosts() or [listen]) if listen == "0.0.0.0" else [listen]
    cli.log_message("[*] 网页控制台已启动。Ctrl+C 停止服务；后台定位会话会继续保持，结束定位请先清除。")
    if os.environ.get("SIMLOCATION_MAP_TOKEN", "").strip():
        cli.log_message("[*] SIMLOCATION_MAP_TOKEN 只用于一次性地图选点，网页控制台不再接受网址里的令牌。")
    if auth.uses_password:
        urls = [f"http://{url_host(host)}:{port}/" for host in hosts]
        cli.log_message(f"[*] 使用用户名「{auth.username}」和密码登录。")
    else:
        code = auth.issue_code()
        urls = [f"http://{url_host(host)}:{port}/login?{urlencode({'code': code})}" for host in hosts]
        cli.log_message("[*] 一次性登录链接：只能用一次，10 分钟内有效。常驻部署请设置 SIMLOCATION_WEB_PASSWORD_HASH。")
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
