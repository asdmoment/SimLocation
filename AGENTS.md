# AGENTS.md

## Purpose

- This file is for coding agents working in this repository.
- Follow the repo's actual structure and avoid inventing tooling that is not present.

## Repository Snapshot

- Main Python CLI: `bin/simlocation.py`; persistent browser API: `bin/simlocation_web.py`
- POSIX shell launcher: `bin/simlocation`; Windows launcher: `bin/simlocation.cmd`
- Browser console: `web/console.html`, `web/console.css`, `web/console.js`
- Map picker pages: `web/map-osm.html`, `web/map-amap.html`; shared route editor: `web/map-route.js`
- AFC helper script: `tools/pm3-afc-sync.sh`
- Unit tests: `tests/test_simlocation.py`, `tests/test_routes.py`, `tests/test_web.py`; browser-side tests: `tests/test_map_routes.cjs` (Node, no deps)
- Example route file: `examples/route.json`
- User docs: `README.md`; release notes: `CHANGELOG.md`; version string: `VERSION`
- Runtime artifacts (gitignored): `var/devices.json`, `var/<UDID>.pid`, `var/<UDID>.state.json`, and `var/simlocation.log` when `--debug` is used

## What This Project Does

- `SimLocation` sets or clears simulated iPhone/iPad location, and can replay a moving route (cross-platform: macOS, Windows, Linux).
- It depends on `pymobiledevice3`, `requests`, a running `tunneld`, and a connected device.
- The Python CLI maintains a background DVT session, and the shell helper handles AFC sync flows.

## Rules Files Present In This Repo

- No `.cursor/rules/` directory was found.
- No `.cursorrules` file was found.
- No `.github/copilot-instructions.md` file was found.

## Tooling Reality

- There is no `pyproject.toml`, `package.json`, `Makefile`, `pytest.ini`, or `tox.ini`.
- There is no repo-defined lint command.
- The committed automated tests use the standard-library `unittest` runner.
- Treat this as a script-first repository with narrow automated and manual verification.

## Environment Assumptions

- Primary OS target is `macOS`; Windows and Linux are also supported.
- Real end-to-end validation requires a connected `iPhone` or `iPad`.
- `tunneld` must be reachable.
- Python must have `requests` and `pymobiledevice3` installed.

## High-Value Commands

- Open the persistent browser console: `bin/simlocation web` (or `web --remote --port 8765`)
- Set a simulated location: `bin/simlocation set <lat> <lon>` (legacy `bin/simlocation <lat> <lon>` still works)
- Clear simulated location: `bin/simlocation clear` (legacy `bin/simlocation --clear` still works)
- Replay a moving route: `bin/simlocation route [file] [--speed KMH] [--speed-noise PERCENT] [--position-noise METERS] [--loop]`; omit the file to draw one on the map
- Target a device: add `--device <alias|UDID>` before or after the subcommand
- Launcher help check when deps are installed: `bin/simlocation --help`; version: `bin/simlocation --version`
- CLI help check when Python deps are installed: `python3 bin/simlocation.py --help`
- AFC helper help: `bash tools/pm3-afc-sync.sh --help`

## Repo Environment Variables

- Local default coordinates: `SIMLOCATION_DEFAULT_LAT`, `SIMLOCATION_DEFAULT_LON`
- Runtime and launcher overrides: `SIMLOCATION_PYTHON`, `SIMLOCATION_VAR_DIR`
- Background startup timeout: `SIMLOCATION_START_TIMEOUT_SECONDS`
- Device and binary overrides: `SIMLOCATION_PMD3`, `SIMLOCATION_UDID`
- tunneld base URL: `SIMLOCATION_TUNNELD_URL`
- Map picker provider key: `SIMLOCATION_AMAP_KEY`
- Map picker binding and access: `SIMLOCATION_MAP_LISTEN`, `SIMLOCATION_MAP_PORT`, `SIMLOCATION_MAP_TIMEOUT_SECONDS`, `SIMLOCATION_MAP_TOKEN`

## Dependency Checks

- Check Python deps explicitly: `python3 -c 'import requests, pymobiledevice3'`

## Verification Commands

- Unit tests: `python3 -m unittest discover -s tests -v` (the interpreter must import `requests` and `pymobiledevice3`; run with the same Python you point `SIMLOCATION_PYTHON` at)
- Browser-side tests: `node tests/test_map_routes.cjs` (stdlib `node:test`; the `.cjs` extension keeps it CommonJS regardless of any `package.json` above the checkout)
- Python syntax check: `python3 -m py_compile bin/simlocation.py bin/simlocation_web.py`
- Browser console syntax check: `node --check web/console.js`
- Shell syntax check: `bash -n bin/simlocation`
- Shell helper syntax check: `bash -n tools/pm3-afc-sync.sh`
- CLI smoke check with deps installed: `python3 bin/simlocation.py --help`
- Read-only environment diagnostic: `bin/simlocation doctor`
- Helper smoke check: `bash tools/pm3-afc-sync.sh --help`

## Single-Test Guidance

- Run one unittest class: `python3 -m unittest tests.test_simlocation.SimLocationSmokeTests -v`
- Run one unittest method: `python3 -m unittest tests.test_simlocation.SimLocationSmokeTests.test_module_exposes_core_cli_boundaries -v`
- For other targeted verification, run the narrowest file-level check that matches your edit.
- Python-only edits: `python3 -m py_compile bin/simlocation.py`
- Launcher-only edits: `bash -n bin/simlocation`
- Helper-script edits: `bash -n tools/pm3-afc-sync.sh`
- Non-destructive helper check: `bash tools/pm3-afc-sync.sh --dry-run --tunnel --push-local <local-path> --push-remote <remote-path>`

## Build Guidance

- There is no build step.
- Do not invent `make build`, `npm run build`, or packaging flows unless you add them and document them.

## Source Of Truth For Behavior

- Start with `README.md` for supported workflows and operator expectations.
- Verify behavior in `bin/simlocation.py` before changing docs.
- Treat `tools/pm3-afc-sync.sh` as the source of truth for AFC helper flags.

## Python Style Guidelines

- Use 4-space indentation.
- Prefer `snake_case` for functions and local variables.
- Use `UPPER_SNAKE_CASE` for module-level constants.
- Group imports sensibly and match surrounding file style; if you touch import blocks, prefer standard library first and third-party second.
- Preserve parenthesized multiline imports and wrapped calls used in `bin/simlocation.py`.
- Prefer `pathlib.Path` over manual string concatenation for filesystem paths.
- Keep runtime file locations under `var/` unless the feature is intentionally configurable.

## Types And Signatures

- Add type hints where they clarify boundaries or return shapes.
- Match surrounding file style instead of doing broad type refactors.
- Keep CLI boundary code straightforward; avoid generic abstractions.

## Naming Conventions

- Keep CLI flags long and descriptive.
- Prefix repo-specific environment variables with `SIMLOCATION_`.
- Include units in timeout constants, for example `*_SECONDS`.
- Prefer verbs for action helpers and nouns for data helpers.

## Error Handling

- Fail fast on invalid CLI input.
- Use `sys.exit(1)` at the CLI boundary when execution cannot continue.
- Raise exceptions inside lower-level helpers when callers need to decide recovery.
- Parse subprocess and JSON output defensively.
- Call `response.raise_for_status()` for HTTP requests that must succeed.

## Logging And State

- Use `log_message()` when a message should be visible and optionally persisted.
- Keep log messages concise and operationally useful.
- Limit broad exception suppression to cleanup paths only.

## Retry And Connectivity Conventions

- Keep retries bounded with named constants.
- Log retry attempts with enough context to diagnose device or tunnel issues.
- Be careful when changing `TUNNELD_URL`; it is a user-environment assumption (overridable via `SIMLOCATION_TUNNELD_URL`).
- Only call tunneld `/cancel` after every tunnel registered for the device failed its reachability probe: `/start-tunnel` hands back a registered tunnel without checking it, so a dead one can only be replaced once cancelled.
- Request a new tunnel with an explicit `connection_type`, one transport at a time (`usbmux`, then `wifi`), and never re-send a request that timed out: the tunnel task keeps running inside tunneld and overlapping requests race in it. A plain `/start-tunnel?udid=` burns its whole timeout in a bonjour scan when the device's tunnel is dead.
- Only use RSD tunnels registered under the target UDID; do not fall back to another device's tunnel.

## Route Playback Noise

- Speed noise is a bounded percentage of the base speed; position noise is a bounded offset radius in meters. Both default to zero and are validated before device work.
- Preserve elapsed-time integration, smooth loop continuity, and the exact held endpoint. Keep source/exported waypoints unchanged.
- `tests/test_routes.py` covers noise bounds, continuity, integration, spherical offsets, worker arguments, and playback; `tests/test_web.py` covers API validation and propagation.

## User-Facing Text

- Existing operator-facing messages in `bin/simlocation.py` are primarily Chinese.
- Internal code identifiers should remain English.

## Shell Script Guidelines

- Keep `bin/simlocation` POSIX-`sh` compatible.
- Keep Bash scripts on `#!/usr/bin/env bash` when they rely on arrays or `[[ ... ]]`.
- Use `set -euo pipefail` for non-trivial Bash scripts.
- Quote variable expansions unless you explicitly need word splitting.
- Prefer arrays for command construction.
- Preserve the existing dry-run pattern for side-effecting helper commands.

## Change Scope Expectations

- Make the smallest change that solves the problem.
- Avoid repo-wide refactors in this small script-based project.
- Do not introduce a dependency manager or linter config unless the task calls for it.

## Secrets And Local Defaults

- Do not hardcode private coordinates in the repository.
- Prefer local environment variables such as `SIMLOCATION_DEFAULT_LAT` and `SIMLOCATION_DEFAULT_LON`.
- Do not commit generated files under `var/`.

## When You Change Behavior

- Update `README.md` if CLI usage, environment variables, or operator workflow changes.
- Update this file if you add real tests, linting, build steps, or new agent rules.
- Mention hardware or macOS-only verification gaps clearly in your final summary.

## Browser Console Boundaries

- Keep device operations in the existing CLI functions; the web API validates input and serializes operations in a worker. Never run interactive device selection in an HTTP handler.
- Keep HTTP status reads independent of device discovery and DVT waits. Include devices with only a runtime state file.
- Reap session children in persistent parents. A successful device clear does not prove the child was reaped; check process exit as well as the state and PID files.
- Authenticate all console pages/assets and API requests, including loopback. API calls require the token header; do not add permissive CORS or log credential-bearing URLs.
- The embedded maps only edit drafts through same-origin, source-checked messages. Device actions require explicit console controls.
- `web` is persistent; legacy `map` and map-based `route` retain one-shot behavior and timeout semantics.
- HTTP tests bind loopback ports and mock every device operation; no real device is required. Browser contract tests use SDK stubs for both providers.

## Recommended Agent Workflow

- Read `README.md`, `bin/simlocation.py`, and any touched script before editing.
- Prefer narrow verification over broad unsupported claims.
