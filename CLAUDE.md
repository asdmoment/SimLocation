# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

SimLocation is a macOS CLI tool that sets simulated GPS locations on connected iPhones/iPads via `pymobiledevice3`. It maintains a background DVT session over a `tunneld` tunnel until the user clears the location.

## Architecture

Two-layer entry point:
- **`bin/simlocation`** — POSIX shell wrapper that resolves symlinks, discovers a suitable Python interpreter (checking `SIMLOCATION_PYTHON`, then `python3` with required deps), and `exec`s into the Python CLI.
- **`bin/simlocation.py`** — Async Python CLI. On `set`, it spawns a detached background process (`--_hold-session`) that opens a DVT connection via `RemoteServiceDiscoveryService` → `DvtSecureSocketProxyService`/`DvtProvider` → `LocationSimulation`, then holds the session until SIGTERM. The foreground process polls `var/simlocation.state.json` for "ready" status and exits.

pymobiledevice3 compatibility: imports are wrapped in `try/except` to support both v8.x (`DvtSecureSocketProxyService`) and v9.x (`DvtProvider`).

Map picker: `simlocation map` starts a temporary HTTP server and opens a browser-based map. Two map providers are supported via separate HTML files:
- **`web/map-osm.html`** — Leaflet + OpenStreetMap (default, no key needed, WGS-84 native)
- **`web/map-amap.html`** — Amap JS API (used when `SIMLOCATION_AMAP_KEY` is set, GCJ-02 → WGS-84 conversion in JS)

Helper script: `tools/pm3-afc-sync.sh` handles AFC file sync (photo export, file push) and is independent of the location CLI.

## Verification Commands

No test suite exists. Use these for validation:

```bash
python3 -m py_compile bin/simlocation.py   # Python syntax
bash -n bin/simlocation                     # Shell wrapper syntax
bash -n tools/pm3-afc-sync.sh              # Helper script syntax
python3 bin/simlocation.py --help           # CLI smoke test (needs pymobiledevice3 + requests)
```

End-to-end testing requires a physical iOS device with Developer Mode enabled and a running `tunneld`.

## Environment Variables

All prefixed with `SIMLOCATION_`:
- `SIMLOCATION_DEFAULT_LAT` / `SIMLOCATION_DEFAULT_LON` — fallback coordinates
- `SIMLOCATION_PYTHON` — override Python interpreter
- `SIMLOCATION_VAR_DIR` — override runtime directory (default: `var/`)
- `SIMLOCATION_PMD3` — override `pymobiledevice3` binary path
- `SIMLOCATION_UDID` — force specific device UDID
- `SIMLOCATION_AMAP_KEY` — Amap JS API key (optional; enables Amap map picker instead of OSM)

## Conventions

- User-facing messages in `bin/simlocation.py` are Chinese; code identifiers are English.
- `bin/simlocation` must stay POSIX `sh` compatible. Bash scripts use `set -euo pipefail`.
- Filesystem paths use `pathlib.Path`. Runtime files go under `var/`.
- No build step, no dependency manager, no linter config. Script-first repo.
- Update `README.md` for usage/env changes; update `AGENTS.md` for tooling/agent rule changes.

## License

GPL-3.0, required by pymobiledevice3's GPL-3.0-or-later license.
