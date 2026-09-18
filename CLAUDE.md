# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

SimLocation is a cross-platform CLI tool (macOS, Windows, Linux) that sets simulated GPS locations on connected iPhones/iPads via `pymobiledevice3`. It maintains a background DVT session over a `tunneld` tunnel until the user clears the location. The same session can replay a moving route instead of holding one point.

## Architecture

Two-layer entry point:
- **`bin/simlocation`** — POSIX shell wrapper (macOS/Linux) that resolves symlinks, discovers a suitable Python interpreter (checking `SIMLOCATION_PYTHON`, then `python3` with required deps), and `exec`s into the Python CLI.
- **`bin/simlocation.cmd`** — Windows batch wrapper with equivalent logic (also tries `python` in addition to `python3`).
- **`bin/simlocation.py`** — Async Python CLI with subcommands: `set`, `route`, `clear`, `map`, `status`, `doctor`, and `device` (`list`/`add`/`remove`/`default`). Shared options `--device` (`-d`), `--connection {auto,rsd}`, `--debug` and `--log-file` are accepted both before and after the subcommand (the subcommand copies use `argparse.SUPPRESS` defaults so they never clobber top-level values). `--version` prints the contents of `VERSION`. Legacy forms `simlocation <lat> <lon>` and `simlocation --clear` still work via `parse_legacy_args`. On `set`, it spawns a detached background process (`--_hold-session`) that opens a DVT connection via `RemoteServiceDiscoveryService` → `DvtSecureSocketProxyService`/`DvtProvider` → `LocationSimulation`, then holds the session until SIGTERM. The foreground process polls per-device state files for "ready" status and exits. Cross-platform: uses `ctypes`/`kernel32` for process management on Windows, `os.kill` signals on Unix; browser detection covers macOS app bundles, Windows `PROGRAMFILES` paths, and Linux `$PATH` lookups.

pymobiledevice3 compatibility: imports are wrapped in `try/except` to support both v8.x (`DvtSecureSocketProxyService`) and v9.x (`DvtProvider`).

Routes: `simlocation route [file]` replays a polyline. `Route` stores waypoints with consecutive duplicates dropped, precomputes cumulative segment lengths, and interpolates with spherical linear interpolation (`Route.position`); antipodal segments are rejected because they have no unique great circle. `load_route` accepts a JSON waypoint list (bare array or `{"points": [...]}`) or a single-segment GPX, capped at `MAX_ROUTE_BYTES`/`MAX_ROUTE_POINTS`; GPX goes through `defusedxml` (a pymobiledevice3 dependency) because stdlib ElementTree expands internal entities. `play_route` derives position from elapsed wall time rather than accumulated steps, so a slow DVT round-trip makes the next update jump ahead instead of drifting behind. `auto_set_route` writes an immutable snapshot of the route into `var/` and passes the path to the detached worker, which loads it during `parse_args` — before reporting ready — so the snapshot is removed as soon as `start_hold_session` returns. `write_state` is atomic (tmp + `Path.replace`) because a moving session rewrites state once a second while `status` reads it.

Map picker: `simlocation map` starts a temporary HTTP server and opens a browser-based map. `--listen`/`--port`/`--no-browser` (or the `--remote` shortcut, equal to `--listen 0.0.0.0 --no-browser`) let a headless host serve the picker to a phone on the same network; a non-loopback bind auto-enables a token that must appear as `?t=` on both `/` and `/confirm`, and the map HTML forwards `window.location.search` so the token survives the POST. Two map providers are supported via separate HTML files:
- **`web/map-osm.html`** — Leaflet + OpenStreetMap (default, no key needed, WGS-84 native)
- **`web/map-amap.html`** — Amap JS API (used when `SIMLOCATION_AMAP_KEY` is set, GCJ-02 → WGS-84 conversion in JS)
- **`web/map-route.js`** — route editing shared by both providers, inlined into the page at `{{ROUTE_SCRIPT}}`. Each provider supplies a draw callback and a to-WGS-84 converter to `initRouteEditor`. `simlocation route` without a file serves the same picker in route mode, and accepts the same `--listen`/`--port`/`--no-browser`/`--remote` options as `map`; the POST body cap rises to `MAX_ROUTE_BYTES` in route mode.

Tunnel acquisition (`acquire_rsd`): `snapshot_rsd_candidates` reads `GET /` from tunneld and returns only the tunnels registered under the target UDID (never another device's). Each candidate is probed with a TCP connect. In `auto` mode, if none is reachable, `cancel_tunnel` sends `GET /cancel?udid=` first, because `/start-tunnel` hands back a registered tunnel without checking that it still works. `request_fresh_rsd` then asks for one transport at a time with an explicit `connection_type`: `usbmux` (`TUNNEL_USBMUX_TIMEOUT_SECONDS`, 10 s) then `wifi` (`TUNNEL_WIFI_TIMEOUT_SECONDS`, 45 s). Their sum stays under `HOLD_START_TIMEOUT_SECONDS`. A plain `/start-tunnel?udid=` is avoided: on a dead tunnel it spends its whole timeout inside a bonjour scan, where an explicit usbmux request answers in ~0.3 s. A request that times out is never re-sent; the snapshot is re-read instead, since the tunnel task keeps running inside tunneld.

Multi-device state: device aliases and the default device are stored in `var/devices.json`. Per-device runtime files use the device UDID as prefix: `var/<UDID>.state.json`, `var/<UDID>.pid`. A single shared debug log `var/simlocation.log` is written only when `--debug` is passed (`--log-file` overrides the path).

Helper script: `tools/pm3-afc-sync.sh` handles AFC file sync (photo export, file push) and is independent of the location CLI.

## Verification Commands

Unit tests live in `tests/test_simlocation.py` and `tests/test_routes.py` (stdlib `unittest`; the module is loaded with `importlib`, so the interpreter must be able to import `requests` and `pymobiledevice3`). Browser-side behavior is covered by `tests/test_map_routes.cjs`, which runs both map HTML files against stubbed Leaflet/Amap SDKs — the `.cjs` extension is deliberate, since a `package.json` with `"type": "module"` anywhere above the checkout would otherwise make Node treat a `.js` test as ESM:

```bash
python3 -m unittest discover -s tests -v    # unit tests (use the same Python as SIMLOCATION_PYTHON)
node tests/test_map_routes.cjs              # browser-side map/route tests (stdlib node:test)
python3 -m py_compile bin/simlocation.py   # Python syntax
bash -n bin/simlocation                     # Shell wrapper syntax
bash -n tools/pm3-afc-sync.sh              # Helper script syntax
python3 bin/simlocation.py --help           # CLI smoke test (needs pymobiledevice3 + requests)
bin/simlocation doctor                      # read-only environment diagnostic
```

The tests never touch a device or tunneld; everything network- or device-facing is patched. End-to-end testing requires a physical iOS device with Developer Mode enabled and a running `tunneld`.

## Environment Variables

All prefixed with `SIMLOCATION_`:
- `SIMLOCATION_DEFAULT_LAT` / `SIMLOCATION_DEFAULT_LON` — fallback coordinates
- `SIMLOCATION_PYTHON` — override Python interpreter
- `SIMLOCATION_VAR_DIR` — override runtime directory (default: `var/`)
- `SIMLOCATION_PMD3` — override `pymobiledevice3` binary path
- `SIMLOCATION_UDID` — force specific device UDID
- `SIMLOCATION_AMAP_KEY` — Amap JS API key (optional; enables Amap map picker instead of OSM)
- `SIMLOCATION_MAP_LISTEN` / `SIMLOCATION_MAP_PORT` — map picker bind address and port (defaults `127.0.0.1` and a random port). Binding a non-loopback address enables token auth automatically.
- `SIMLOCATION_MAP_TOKEN` — fixed access token for the map picker; without it a one-shot token is generated per run. Only used when the bind address is non-loopback.
- `SIMLOCATION_MAP_TIMEOUT_SECONDS` — how long the picker stays open (default 300)
- `SIMLOCATION_START_TIMEOUT_SECONDS` — how long the foreground `set` waits for the background session to become ready (default 60; must stay above the 45 s tunnel request timeout)
- `SIMLOCATION_TUNNELD_URL` — tunneld base URL (default `http://127.0.0.1:49151`)

## Conventions

- User-facing messages in `bin/simlocation.py` are Chinese; code identifiers are English.
- `bin/simlocation` must stay POSIX `sh` compatible. Bash scripts use `set -euo pipefail`.
- Filesystem paths use `pathlib.Path`. Runtime files go under `var/`.
- No build step, no dependency manager, no linter config. Script-first repo.
- Update `README.md` for usage/env changes; update `AGENTS.md` for tooling/agent rule changes.

## License

GPL-3.0, required by pymobiledevice3's GPL-3.0-or-later license.
