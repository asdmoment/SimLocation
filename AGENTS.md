# AGENTS.md

## Purpose

- This file is for coding agents working in this repository.
- Follow the repo's actual structure and avoid inventing tooling that is not present.

## Repository Snapshot

- Main Python CLI: `bin/simlocation.py`
- POSIX shell launcher: `bin/simlocation`
- AFC helper script: `tools/pm3-afc-sync.sh`
- Root helper symlink: `pm3-afc-sync.sh -> tools/pm3-afc-sync.sh`
- User docs: `README.md`
- Runtime artifacts: `var/simlocation.log`, `var/simlocation.pid`, `var/simlocation.state.json`

## What This Project Does

- `SimLocation` sets or clears simulated iPhone/iPad location (cross-platform: macOS, Windows, Linux).
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

- Run the main CLI through the launcher: `bin/simlocation <lat> <lon>`
- Clear simulated location: `bin/simlocation --clear`
- Launcher help check when deps are installed: `bin/simlocation --help`
- CLI help check when Python deps are installed: `python3 bin/simlocation.py --help`
- AFC helper help: `bash tools/pm3-afc-sync.sh --help`

## Repo Environment Variables

- Local default coordinates: `SIMLOCATION_DEFAULT_LAT`, `SIMLOCATION_DEFAULT_LON`
- Runtime and launcher overrides: `SIMLOCATION_PYTHON`, `SIMLOCATION_VAR_DIR`
- Device and binary overrides: `SIMLOCATION_PMD3`, `SIMLOCATION_UDID`

## Dependency Checks

- Check Python deps explicitly: `python3 -c 'import requests, pymobiledevice3'`

## Verification Commands

- Unit tests: `python3 -m unittest discover -s tests -v`
- Python syntax check: `python3 -m py_compile bin/simlocation.py`
- Shell syntax check: `bash -n bin/simlocation`
- Shell helper syntax check: `bash -n tools/pm3-afc-sync.sh`
- CLI smoke check with deps installed: `python3 bin/simlocation.py --help`
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
- Be careful when changing `TUNNELD_URL`; it is a user-environment assumption.

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

## Recommended Agent Workflow

- Read `README.md`, `bin/simlocation.py`, and any touched script before editing.
- Prefer narrow verification over broad unsupported claims.
