# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

SimLocation is a cross-platform CLI tool (macOS, Windows, Linux) that sets simulated GPS locations on connected iPhones/iPads via `pymobiledevice3`. It maintains a background DVT session over a `tunneld` tunnel until the user clears the location.

## Architecture

Two-layer entry point:
- **`bin/simlocation`** — POSIX shell wrapper (macOS/Linux) that resolves symlinks, discovers a suitable Python interpreter (checking `SIMLOCATION_PYTHON`, then `python3` with required deps), and `exec`s into the Python CLI.
- **`bin/simlocation.cmd`** — Windows batch wrapper with equivalent logic (also tries `python` in addition to `python3`).
- **`bin/simlocation.py`** — Async Python CLI with subcommands: `set`, `clear`, `map`, `status`, `doctor`, and `device` (`list`/`add`/`remove`/`default`). Shared options `--device` (`-d`), `--connection {auto,rsd}`, `--debug` and `--log-file` are accepted both before and after the subcommand (the subcommand copies use `argparse.SUPPRESS` defaults so they never clobber top-level values). `--version` prints the contents of `VERSION`. Legacy forms `simlocation <lat> <lon>` and `simlocation --clear` still work via `parse_legacy_args`. On `set`, it spawns a detached background process (`--_hold-session`) that opens a DVT connection via `RemoteServiceDiscoveryService` → `DvtSecureSocketProxyService`/`DvtProvider` → `LocationSimulation`, then holds the session until SIGTERM. The foreground process polls per-device state files for "ready" status and exits. Cross-platform: uses `ctypes`/`kernel32` for process management on Windows, `os.kill` signals on Unix; browser detection covers macOS app bundles, Windows `PROGRAMFILES` paths, and Linux `$PATH` lookups.

pymobiledevice3 compatibility: imports are wrapped in `try/except` to support both v8.x (`DvtSecureSocketProxyService`) and v9.x (`DvtProvider`).

Map picker: `simlocation map` starts a temporary HTTP server and opens a browser-based map. Two map providers are supported via separate HTML files:
- **`web/map-osm.html`** — Leaflet + OpenStreetMap (default, no key needed, WGS-84 native)
- **`web/map-amap.html`** — Amap JS API (used when `SIMLOCATION_AMAP_KEY` is set, GCJ-02 → WGS-84 conversion in JS)

Tunnel acquisition (`acquire_rsd`): `snapshot_rsd_candidates` reads `GET /` from tunneld and returns only the tunnels registered under the target UDID (never another device's). Each candidate is probed with a TCP connect; in `auto` mode, if none is reachable, `request_fresh_rsd` issues a single `GET /start-tunnel?udid=` with a 45 s timeout (`TUNNEL_START_TIMEOUT_SECONDS`). tunneld tries usbmux/USB/Wi-Fi itself and blocks until the tunnel is up, so the request is not repeated on timeout; the snapshot is re-read instead. `/cancel` is never called.

Multi-device state: device aliases and the default device are stored in `var/devices.json`. Per-device runtime files use the device UDID as prefix: `var/<UDID>.state.json`, `var/<UDID>.pid`. A single shared debug log `var/simlocation.log` is written only when `--debug` is passed (`--log-file` overrides the path).

Helper script: `tools/pm3-afc-sync.sh` handles AFC file sync (photo export, file push) and is independent of the location CLI.

## Verification Commands

Unit tests live in `tests/test_simlocation.py` (stdlib `unittest`; the module is loaded with `importlib`, so the interpreter must be able to import `requests` and `pymobiledevice3`):

```bash
python3 -m unittest discover -s tests -v    # unit tests (use the same Python as SIMLOCATION_PYTHON)
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
