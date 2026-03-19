# Map Picker v2.0.0 Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add a `simlocation map` command that opens a browser-based Amap picker, returning WGS-84 coordinates to the CLI for automatic or manual use.

**Architecture:** A temporary Python HTTP server serves `web/map.html` with the Amap JS API key injected. The page POSTs coordinates back to the server, which either calls the existing `auto_set_location()` or prints them. The existing CLI is refactored from positional args to subcommands while preserving backward compatibility.

**Tech Stack:** Python stdlib (`http.server`, `webbrowser`, `threading`), Amap JS API v2.0 (browser-side), inline GCJ-02→WGS-84 conversion in JS.

---

### Task 1: Create `web/map.html` — the Amap picker page

**Files:**
- Create: `web/map.html`

**Step 1: Write the HTML file**

Create `web/map.html` with the following:

```html
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SimLocation — 地图选点</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  html, body { height: 100%; font-family: -apple-system, "PingFang SC", "Helvetica Neue", sans-serif; }
  #map { width: 100%; height: 100%; }

  #panel {
    position: absolute; bottom: 20px; left: 50%; transform: translateX(-50%);
    background: #fff; border-radius: 12px; box-shadow: 0 4px 20px rgba(0,0,0,.15);
    padding: 16px 24px; display: flex; align-items: center; gap: 16px;
    z-index: 1000; min-width: 400px;
  }
  #coords { font-size: 14px; line-height: 1.6; flex: 1; }
  #coords .label { color: #888; font-size: 12px; }
  #coords .value { font-family: "SF Mono", Menlo, monospace; font-size: 15px; }
  #confirm-btn {
    background: #1677ff; color: #fff; border: none; border-radius: 8px;
    padding: 10px 28px; font-size: 15px; cursor: pointer; white-space: nowrap;
  }
  #confirm-btn:hover { background: #0958d9; }
  #confirm-btn:disabled { background: #d9d9d9; cursor: not-allowed; }
  #done-msg { display: none; color: #52c41a; font-weight: 600; font-size: 15px; }

  #search-box {
    position: absolute; top: 16px; left: 16px; z-index: 1000;
    display: flex; gap: 8px;
  }
  #search-input {
    width: 280px; padding: 8px 14px; border: none; border-radius: 8px;
    box-shadow: 0 2px 12px rgba(0,0,0,.12); font-size: 14px; outline: none;
  }
</style>
</head>
<body>
<div id="map"></div>

<div id="search-box">
  <input id="search-input" type="text" placeholder="搜索地点…">
</div>

<div id="panel">
  <div id="coords">
    <div><span class="label">GCJ-02: </span><span id="gcj-value" class="value">—</span></div>
    <div><span class="label">WGS-84: </span><span id="wgs-value" class="value">—</span></div>
  </div>
  <button id="confirm-btn" disabled>确认选点</button>
  <span id="done-msg">已提交</span>
</div>

<script>
// ——— GCJ-02 → WGS-84 转换 ———
const PI = Math.PI;
const A = 6378245.0;
const EE = 0.00669342162296594323;

function outOfChina(lng, lat) {
  return lng < 72.004 || lng > 137.8347 || lat < 0.8293 || lat > 55.8271;
}

function transformLat(x, y) {
  let r = -100.0 + 2.0*x + 3.0*y + 0.2*y*y + 0.1*x*y + 0.2*Math.sqrt(Math.abs(x));
  r += (20.0*Math.sin(6.0*x*PI) + 20.0*Math.sin(2.0*x*PI)) * 2.0/3.0;
  r += (20.0*Math.sin(y*PI) + 40.0*Math.sin(y/3.0*PI)) * 2.0/3.0;
  r += (160.0*Math.sin(y/12.0*PI) + 320.0*Math.sin(y*PI/30.0)) * 2.0/3.0;
  return r;
}

function transformLng(x, y) {
  let r = 300.0 + x + 2.0*y + 0.1*x*x + 0.1*x*y + 0.1*Math.sqrt(Math.abs(x));
  r += (20.0*Math.sin(6.0*x*PI) + 20.0*Math.sin(2.0*x*PI)) * 2.0/3.0;
  r += (20.0*Math.sin(x*PI) + 40.0*Math.sin(x/3.0*PI)) * 2.0/3.0;
  r += (150.0*Math.sin(x/12.0*PI) + 300.0*Math.sin(x/30.0*PI)) * 2.0/3.0;
  return r;
}

function gcj02ToWgs84(lng, lat) {
  if (outOfChina(lng, lat)) return [lng, lat];
  let dlat = transformLat(lng - 105.0, lat - 35.0);
  let dlng = transformLng(lng - 105.0, lat - 35.0);
  const radlat = lat / 180.0 * PI;
  let magic = Math.sin(radlat);
  magic = 1 - EE * magic * magic;
  const sqrtmagic = Math.sqrt(magic);
  dlat = (dlat * 180.0) / ((A * (1 - EE)) / (magic * sqrtmagic) * PI);
  dlng = (dlng * 180.0) / (A / sqrtmagic * Math.cos(radlat) * PI);
  return [lng - dlng, lat - dlat];
}

// ——— State ———
let marker = null;
let currentWgs = null;
const SERVER_PORT = "{{PORT}}";

// ——— Map init ———
window._onAMapLoaded = function() {
  const map = new AMap.Map("map", {
    zoom: 13,
    center: [116.397, 39.909],
    mapStyle: "amap://styles/normal",
  });

  // POI search
  const input = document.getElementById("search-input");
  AMap.plugin("AMap.PlaceSearch", function() {
    const placeSearch = new AMap.PlaceSearch({ pageSize: 1 });
    input.addEventListener("keydown", function(e) {
      if (e.key !== "Enter") return;
      const kw = input.value.trim();
      if (!kw) return;
      placeSearch.search(kw, function(status, result) {
        if (status === "complete" && result.poiList && result.poiList.pois.length) {
          const poi = result.poiList.pois[0];
          const lnglat = poi.location;
          map.setCenter(lnglat);
          map.setZoom(16);
          placeMarker(map, lnglat.lng, lnglat.lat);
        }
      });
    });
  });

  map.on("click", function(e) {
    placeMarker(map, e.lnglat.lng, e.lnglat.lat);
  });
};

function placeMarker(map, lng, lat) {
  if (marker) marker.setMap(null);
  marker = new AMap.Marker({ position: [lng, lat], map: map });
  const wgs = gcj02ToWgs84(lng, lat);
  currentWgs = { lat: wgs[1], lon: wgs[0] };
  document.getElementById("gcj-value").textContent =
    lat.toFixed(6) + ", " + lng.toFixed(6);
  document.getElementById("wgs-value").textContent =
    wgs[1].toFixed(6) + ", " + wgs[0].toFixed(6);
  document.getElementById("confirm-btn").disabled = false;
}

// ——— Confirm ———
document.getElementById("confirm-btn").addEventListener("click", function() {
  if (!currentWgs) return;
  const btn = this;
  btn.disabled = true;
  fetch("http://127.0.0.1:" + SERVER_PORT + "/confirm", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(currentWgs),
  }).then(function(r) {
    if (r.ok) {
      btn.style.display = "none";
      document.getElementById("done-msg").style.display = "inline";
    } else {
      btn.disabled = false;
      alert("提交失败，请重试。");
    }
  }).catch(function() {
    btn.disabled = false;
    alert("无法连接到本地服务，请检查终端。");
  });
});
</script>
<script>
(function() {
  var s = document.createElement("script");
  s.src = "https://webapi.amap.com/maps?v=2.0&key={{AMAP_KEY}}&callback=_onAMapLoaded";
  document.head.appendChild(s);
})();
</script>
</body>
</html>
```

Placeholders `{{PORT}}` and `{{AMAP_KEY}}` will be replaced at serve time by the Python server.

**Step 2: Verify HTML is valid**

Run: `python3 -c "from pathlib import Path; h = Path('web/map.html').read_text(); assert '{{PORT}}' in h and '{{AMAP_KEY}}' in h; print('OK')"`
Expected: `OK`

**Step 3: Commit**

```bash
git add web/map.html
git commit -m "feat: add Amap picker HTML page for map subcommand"
```

---

### Task 2: Add map server and `map` subcommand to `simlocation.py`

**Files:**
- Modify: `bin/simlocation.py`

**Step 1: Add imports at top of file (after existing imports, around line 16)**

Add these stdlib imports (they are not already present):

```python
import threading
import webbrowser
from http.server import HTTPServer, BaseHTTPRequestHandler
from functools import partial
```

**Step 2: Add the Amap Key guide text and map server class**

Insert before `parse_args()` (around line 680):

```python
AMAP_KEY_GUIDE = """\
[!] 未设置高德地图 Key。

  使用地图选点功能需要一个免费的高德 JS API Key：

  1. 前往 https://console.amap.com/ 注册/登录
  2. 进入「应用管理」→「我的应用」→「创建新应用」
  3. 为应用添加一个 Key，服务平台选择「Web端(JS API)」
  4. 复制 Key，在终端中设置环境变量：

     export SIMLOCATION_AMAP_KEY=你的Key

  设置完成后重新运行 simlocation map。
"""

MAP_HTML_PATH = PROJECT_DIR / "web" / "map.html"
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
        pass  # suppress default stderr logging


def run_map_picker(amap_key):
    """Start local server, open browser, block until user picks a point.
    Returns (lat, lon) as floats, or None on timeout."""
    if not MAP_HTML_PATH.is_file():
        print(f"[!] 地图页面文件不存在: {MAP_HTML_PATH}")
        sys.exit(1)
    template = MAP_HTML_PATH.read_text(encoding="utf-8")

    server = HTTPServer(("127.0.0.1", 0), _MapRequestHandler)
    port = server.server_address[1]
    html_text = template.replace("{{AMAP_KEY}}", amap_key).replace("{{PORT}}", str(port))
    server.map_html = html_text.encode("utf-8")
    server.picked_coords = None

    url = f"http://127.0.0.1:{port}/"
    print(f"[*] 地图选点服务已启动: {url}")
    print("[*] 正在打开浏览器，请在地图上选择位置后点击「确认选点」。")
    webbrowser.open(url)

    server.timeout = MAP_SERVER_TIMEOUT_SECONDS
    timer = threading.Timer(MAP_SERVER_TIMEOUT_SECONDS, server.shutdown)
    timer.daemon = True
    timer.start()

    server.serve_forever()
    timer.cancel()
    return server.picked_coords
```

**Step 3: Rewrite `parse_args()` and `__main__` block to support subcommands + backward compat**

Replace `parse_args()` (line 681-716) and the `if __name__ == "__main__"` block (line 719-763) with:

```python
def parse_args():
    parser = argparse.ArgumentParser(
        prog="simlocation",
        description="通过 pymobiledevice3 自动设置 iPhone 虚拟定位。",
    )
    sub = parser.add_subparsers(dest="command")

    # --- set ---
    p_set = sub.add_parser("set", help="设置虚拟定位")
    p_set.add_argument("lat", help="纬度")
    p_set.add_argument("lon", help="经度")

    # --- clear ---
    sub.add_parser("clear", help="清除虚拟定位，恢复真实位置")

    # --- map ---
    p_map = sub.add_parser("map", help="打开地图选点")
    p_map.add_argument(
        "--pick-only",
        action="store_true",
        help="仅输出选中的坐标，不自动设置定位",
    )

    # --- shared flags (on parent parser) ---
    parser.add_argument("--clear", action="store_true", dest="legacy_clear",
                        help=argparse.SUPPRESS)
    parser.add_argument("--debug", action="store_true",
                        help="记录详细日志到文件")
    parser.add_argument("--log-file", default=str(DEFAULT_LOG_PATH),
                        help=argparse.SUPPRESS)
    parser.add_argument("--connection", choices=("auto", "rsd"), default="auto",
                        help="连接模式")
    parser.add_argument("--_hold-session", action="store_true",
                        help=argparse.SUPPRESS)
    parser.add_argument("--pid-file", default=str(DEFAULT_PID_PATH),
                        help=argparse.SUPPRESS)
    parser.add_argument("--state-file", default=str(DEFAULT_STATE_PATH),
                        help=argparse.SUPPRESS)

    args, remaining = parser.parse_known_args()

    # Backward compat: simlocation <lat> <lon>
    if args.command is None and not args.legacy_clear and remaining:
        if len(remaining) >= 2:
            args.command = "set"
            args.lat = remaining[0]
            args.lon = remaining[1]
        elif len(remaining) == 1:
            parser.error(f"未识别的参数: {remaining[0]}")

    # Backward compat: simlocation --clear
    if args.command is None and args.legacy_clear:
        args.command = "clear"

    # No command and no legacy args → default to set with env coords
    if args.command is None:
        env_lat = os.environ.get("SIMLOCATION_DEFAULT_LAT")
        env_lon = os.environ.get("SIMLOCATION_DEFAULT_LON")
        if env_lat is not None and env_lon is not None:
            args.command = "set"
            args.lat = env_lat
            args.lon = env_lon
        else:
            parser.print_help()
            sys.exit(1)

    return args


if __name__ == "__main__":
    args = parse_args()

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
        run_hold_session(
            args.lat, args.lon, pmd3_bin, args.connection,
            pid_path, state_path, log_path,
        )
        sys.exit(0)

    if args.command == "clear":
        clear_location(pmd3_bin, args.connection, log_path, pid_path, state_path)

    elif args.command == "map":
        amap_key = os.environ.get("SIMLOCATION_AMAP_KEY", "").strip()
        if not amap_key:
            print(AMAP_KEY_GUIDE)
            sys.exit(1)
        coords = run_map_picker(amap_key)
        if coords is None:
            print("[-] 未选择坐标（超时或已关闭）。")
            sys.exit(1)
        lat, lon = coords
        pick_only = getattr(args, "pick_only", False)
        if pick_only:
            print(f"{lat:.6f} {lon:.6f}")
        else:
            print(f"[*] 已选择坐标: {lat:.6f}, {lon:.6f}")
            auto_set_location(
                str(lat), str(lon), pmd3_bin, args.connection,
                log_path, pid_path, state_path,
            )

    elif args.command == "set":
        auto_set_location(
            args.lat, args.lon, pmd3_bin, args.connection,
            log_path, pid_path, state_path,
        )
```

**Step 4: Remove the now-unused `resolve_target_coordinates()` function**

Delete `resolve_target_coordinates()` (lines 656-678) and `exit_coordinate_resolution_error()` (lines 651-653). The logic is now handled inside `parse_args()`.

**Step 5: Verify syntax**

Run: `python3 -m py_compile bin/simlocation.py`
Expected: no output (success)

**Step 6: Verify help output**

Run: `python3 bin/simlocation.py --help`
Expected: shows subcommands `set`, `clear`, `map`

Run: `python3 bin/simlocation.py map --help`
Expected: shows `--pick-only` flag

**Step 7: Commit**

```bash
git add bin/simlocation.py
git commit -m "feat: add map subcommand with local HTTP server and Amap picker"
```

---

### Task 3: Update VERSION, CHANGELOG, README

**Files:**
- Modify: `VERSION`
- Modify: `CHANGELOG.md`
- Modify: `README.md`

**Step 1: Update VERSION**

Replace contents of `VERSION` with:
```
v2.0.0
```

**Step 2: Update CHANGELOG.md**

Prepend to `CHANGELOG.md` after the `# Changelog` heading:

```markdown
## v2.0.0

- 新增 `simlocation map` 子命令：在浏览器中打开高德地图选点页面，点选位置后自动设置虚拟定位。
- 支持 `--pick-only` 模式，仅输出坐标不设置定位，便于脚本集成。
- 内置 GCJ-02 → WGS-84 坐标转换，确保发送给设备的坐标准确。
- CLI 重构为子命令模式（`set`/`clear`/`map`），同时保持 `simlocation <lat> <lon>` 和 `simlocation --clear` 的向后兼容。
- 需要高德 JS API Key（免费申请），通过 `SIMLOCATION_AMAP_KEY` 环境变量配置。
```

**Step 3: Update README.md**

Add a new section after "## 坐标用法" section. The section should include:

```markdown
## 地图选点

除了手动输入坐标，你还可以通过浏览器地图来选择位置：

```bash
$ simlocation map
```

运行后会在浏览器中打开一个高德地图页面。在地图上点击选择位置，确认后会自动设置虚拟定位。

如果你只想获取坐标而不设置定位（比如用于脚本）：

```bash
$ simlocation map --pick-only
```

### 配置高德 Key

地图选点功能需要一个高德 JS API Key（免费）：

1. 前往 [高德开放平台](https://console.amap.com/) 注册或登录
2. 进入「应用管理」→「我的应用」→ 创建新应用
3. 为应用添加一个 Key，服务平台选择「Web端(JS API)」
4. 复制 Key，设置环境变量：

```bash
$ export SIMLOCATION_AMAP_KEY=你的Key
```

建议将上面这行加入 `~/.zshrc` 或 `~/.bashrc` 以便长期使用。
```

**Step 4: Verify shell wrapper syntax unchanged**

Run: `bash -n bin/simlocation`
Expected: no output (success)

**Step 5: Commit**

```bash
git add VERSION CHANGELOG.md README.md
git commit -m "docs: update VERSION, CHANGELOG, README for v2.0.0 map picker release"
```

---

### Task 4: End-to-end smoke test

**Step 1: Verify syntax of all scripts**

Run:
```bash
python3 -m py_compile bin/simlocation.py && echo "py OK"
bash -n bin/simlocation && echo "sh OK"
```
Expected: `py OK` and `sh OK`

**Step 2: Verify help output**

Run: `python3 bin/simlocation.py --help`
Expected: shows `{set,clear,map}` subcommands

**Step 3: Verify map without key shows guide**

Run: `SIMLOCATION_AMAP_KEY= python3 bin/simlocation.py map 2>&1; echo "exit=$?"`
Expected: prints the Amap Key guide text, `exit=1`

**Step 4: Verify backward compat**

Run: `python3 bin/simlocation.py --help` — should still work
Run: `python3 bin/simlocation.py 39.9 116.3 --help 2>&1 || true` — should not crash on parse

**Step 5: (Manual) Test with real Amap key**

Set `SIMLOCATION_AMAP_KEY` to a valid key and run:
```bash
simlocation map --pick-only
```
Expected: browser opens, map loads, click a point, confirm, terminal prints `lat lon`.

---

Plan complete and saved to `docs/plans/2026-03-19-map-picker-plan.md`. Two execution options:

**1. Subagent-Driven (this session)** - I dispatch fresh subagent per task, review between tasks, fast iteration

**2. Parallel Session (separate)** - Open new session with executing-plans, batch execution with checkpoints

Which approach?
