#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
One-click AFC sync for pymobiledevice3:
  1) Export photos from /DCIM
  2) Push a local file (or folder) back to iPhone

Usage:
  pm3-afc-sync.sh [options]

Connection options (pick one):
  --rsd-host HOST --rsd-port PORT   Use new tunnel RSD endpoint
  --tunnel                          Use running tunneld mode

Task options:
  --photo-out DIR                   Photo export directory
                                    (default: ~/Desktop/iPhone-DCIM-YYYYMMDD_HHMMSS)
  --push-local PATH                 Local file/folder to upload (required)
  --push-remote PATH                Remote destination path on iPhone (required)
  --dry-run                         Print commands only, do not execute

Examples:
  pm3-afc-sync.sh \
    --rsd-host fd7b:e5b:6f53::1 --rsd-port 64337 \
    --push-local "$HOME/Desktop/test.txt" \
    --push-remote "/Books/test.txt"

  pm3-afc-sync.sh \
    --tunnel \
    --photo-out "$HOME/Desktop/my-dcim" \
    --push-local "$HOME/Desktop/data" \
    --push-remote "/Books/data"
EOF
}

fail() {
  echo "Error: $*" >&2
  exit 1
}

choose_pm3() {
  if command -v pymobiledevice3 >/dev/null 2>&1; then
    PM3_CMD=(pymobiledevice3)
  else
    PM3_CMD=(python3 -m pymobiledevice3)
  fi
}

RSD_HOST=""
RSD_PORT=""
USE_TUNNEL=0
PUSH_LOCAL=""
PUSH_REMOTE=""
DRY_RUN=0
PHOTO_OUT_DEFAULT="$HOME/Desktop/iPhone-DCIM-$(date +%Y%m%d_%H%M%S)"
PHOTO_OUT="$PHOTO_OUT_DEFAULT"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --rsd-host)
      [[ $# -ge 2 ]] || fail "--rsd-host requires a value"
      RSD_HOST="$2"
      shift 2
      ;;
    --rsd-port)
      [[ $# -ge 2 ]] || fail "--rsd-port requires a value"
      RSD_PORT="$2"
      shift 2
      ;;
    --tunnel)
      USE_TUNNEL=1
      shift
      ;;
    --photo-out)
      [[ $# -ge 2 ]] || fail "--photo-out requires a value"
      PHOTO_OUT="$2"
      shift 2
      ;;
    --push-local)
      [[ $# -ge 2 ]] || fail "--push-local requires a value"
      PUSH_LOCAL="$2"
      shift 2
      ;;
    --push-remote)
      [[ $# -ge 2 ]] || fail "--push-remote requires a value"
      PUSH_REMOTE="$2"
      shift 2
      ;;
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      fail "Unknown argument: $1 (use --help)"
      ;;
  esac
done

[[ -n "$PUSH_LOCAL" ]] || fail "--push-local is required"
[[ -n "$PUSH_REMOTE" ]] || fail "--push-remote is required"
[[ -e "$PUSH_LOCAL" ]] || fail "--push-local path does not exist: $PUSH_LOCAL"

if [[ "$USE_TUNNEL" -eq 1 ]]; then
  [[ -z "$RSD_HOST" && -z "$RSD_PORT" ]] || fail "Use either --tunnel OR --rsd-host/--rsd-port, not both"
else
  [[ -n "$RSD_HOST" && -n "$RSD_PORT" ]] || fail "Provide --rsd-host and --rsd-port, or use --tunnel"
fi

choose_pm3
PM3_CONN_ARGS=()
if [[ "$USE_TUNNEL" -eq 1 ]]; then
  PM3_CONN_ARGS=(--tunnel)
else
  PM3_CONN_ARGS=(--rsd "$RSD_HOST" "$RSD_PORT")
fi

mkdir -p "$PHOTO_OUT"

run_cmd() {
  if [[ "$DRY_RUN" -eq 1 ]]; then
    printf '[dry-run] '
    printf '%q ' "$@"
    printf '\n'
  else
    "$@"
  fi
}

echo "==> Exporting photos from /DCIM to: $PHOTO_OUT"
run_cmd "${PM3_CMD[@]}" afc pull "/DCIM" "$PHOTO_OUT" "${PM3_CONN_ARGS[@]}"

echo "==> Uploading: $PUSH_LOCAL -> $PUSH_REMOTE"
run_cmd "${PM3_CMD[@]}" afc push "$PUSH_LOCAL" "$PUSH_REMOTE" "${PM3_CONN_ARGS[@]}"

echo "Done."
