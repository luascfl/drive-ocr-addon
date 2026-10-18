#!/usr/bin/env bash
# Deploys the checked-out OCR server to the existing Oracle VM without interrupting an OCR job.
# It deliberately does not update Apps Script: the add-on source is in addon/Code.gs, but the repository has no
# Apps Script deployment credential. Paste that file in the editor after this script reports success.
#
# usage: ./deploy/update.sh [--check] [--dry-run] [--wait SECONDS]
#
# Overrides for a recreated VM or a nonstandard workstation:
#   OCR_IP_FILE, OCR_HOST_FILE, OCR_SSH_KEY, OCR_REMOTE_USER
#
# The cached host and IP are operator-local, intentionally outside git. The HTTPS health check is pinned to the cached
# IP with curl --resolve: a stale nip.io DNS answer cannot make the script inspect or deploy to another server.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVER_SRC="$ROOT/server/ocr_server.py"
VERSION_FILE="$ROOT/VERSION"
ADDON_SRC="$ROOT/addon/Code.gs"
IP_FILE="${OCR_IP_FILE:-$HOME/.cache/ocr_micro_ip}"
HOST_FILE="${OCR_HOST_FILE:-$HOME/.cache/ocr_host}"
SSH_KEY="${OCR_SSH_KEY:-$HOME/.ssh/oci_console_rsa}"
REMOTE_USER="${OCR_REMOTE_USER:-ubuntu}"
CHECK_ONLY=0
DRY_RUN=0
WAIT_SECONDS=0

usage() {
  cat <<'EOF'
usage: ./deploy/update.sh [--check] [--dry-run] [--wait SECONDS]

  --check          inspect the release and server health; never uploads or restarts
  --dry-run        validate local release files and print the deployment plan; no network action
  --wait SECONDS   wait up to SECONDS for busy=false and queued=0 before deploying (default: fail immediately)
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --check) CHECK_ONLY=1 ;;
    --dry-run) DRY_RUN=1 ;;
    --wait)
      [ "$#" -ge 2 ] || { echo "--wait needs a number of seconds" >&2; exit 2; }
      WAIT_SECONDS="$2"; shift ;;
    --help|-h) usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done
case "$WAIT_SECONDS" in ''|*[!0-9]*) echo "--wait must be a non-negative integer" >&2; exit 2 ;; esac

need() { command -v "$1" >/dev/null || { echo "missing required command: $1" >&2; exit 2; }; }
for command in curl python3 scp ssh; do need "$command"; done
[ -f "$SERVER_SRC" ] || { echo "missing $SERVER_SRC" >&2; exit 2; }
[ -f "$VERSION_FILE" ] || { echo "missing $VERSION_FILE" >&2; exit 2; }
[ -f "$ADDON_SRC" ] || { echo "missing $ADDON_SRC" >&2; exit 2; }
[ -f "$IP_FILE" ] || { echo "missing cached VM IP: $IP_FILE" >&2; exit 2; }
[ -f "$HOST_FILE" ] || { echo "missing cached VM host: $HOST_FILE" >&2; exit 2; }
[ -r "$SSH_KEY" ] || { echo "missing SSH key: $SSH_KEY" >&2; exit 2; }

VERSION="$(tr -d '\r\n' < "$VERSION_FILE")"
case "$VERSION" in *[!0-9.]*|'') echo "VERSION must contain only digits and dots, got: $VERSION" >&2; exit 2 ;; esac
python3 - "$VERSION" "$SERVER_SRC" "$ADDON_SRC" <<'PY'
import re
import sys
version, server_path, addon_path = sys.argv[1:]
if not re.fullmatch(r"\d+\.\d+\.\d+", version):
    raise SystemExit(f"VERSION must be a semantic version, got: {version}")
server = open(server_path, encoding="utf-8").read()
addon = open(addon_path, encoding="utf-8").read()
checks = [
    (r'^VERSION = "' + re.escape(version) + r'"(?:\s|#)', server, "server VERSION"),
    (r"^var OCR_VERSION = '" + re.escape(version) + r"';\s*$", addon, "add-on OCR_VERSION"),
]
for pattern, contents, label in checks:
    if not re.search(pattern, contents, re.M):
        raise SystemExit(f"{label} does not equal VERSION ({version})")
PY

IP="$(tr -d '[:space:]' < "$IP_FILE")"
HOST="$(tr -d '[:space:]' < "$HOST_FILE")"
case "$IP" in *[!0-9.]*|*.*.*.*.*|.*|*.) echo "invalid cached IP: $IP" >&2; exit 2 ;; esac
case "$HOST" in ''|*'/'*|*' '* ) echo "invalid cached host: $HOST" >&2; exit 2 ;; esac

print_addon_step() {
  cat <<EOF
Add-on update still required:
  1. Open the Apps Script editor for OCR for Drive.
  2. Replace Code.gs with addon/Code.gs after replacing __OCR_URL__ with https://$HOST.
  3. Save, reopen the Drive panel and check Version $VERSION.
EOF
}

if [ "$DRY_RUN" = 1 ]; then
  echo "Dry run: local release $VERSION is consistent."
  echo "Would check https://$HOST/health pinned to $IP, upload $SERVER_SRC, restart ocr, and require health version $VERSION."
  print_addon_step
  exit 0
fi

health() {
  curl --fail --silent --show-error --max-time 15 --resolve "$HOST:443:$IP" "https://$HOST/health"
}
health_state() {
  python3 -c 'import json,sys; d=json.load(sys.stdin); print(str(bool(d.get("busy"))).lower(), d.get("queued"), d.get("version", ""))'
}

deadline=$(( $(date +%s) + WAIT_SECONDS ))
while :; do
  STATUS="$(health)" || { echo "health check failed for https://$HOST (pinned to $IP); nothing deployed" >&2; exit 1; }
  read -r BUSY QUEUED REMOTE_VERSION <<< "$(printf '%s' "$STATUS" | health_state)"
  case "$QUEUED" in ''|*[!0-9]*) echo "invalid health response: $STATUS" >&2; exit 1 ;; esac
  if [ "$BUSY" = false ] && [ "$QUEUED" = 0 ]; then break; fi
  if [ "$WAIT_SECONDS" = 0 ] || [ "$(date +%s)" -ge "$deadline" ]; then
    echo "OCR service is busy=$BUSY queued=$QUEUED; nothing deployed. Retry later or use --wait SECONDS." >&2
    exit 1
  fi
  echo "OCR service is busy=$BUSY queued=$QUEUED; waiting 10 s..."
  sleep 10
done

if [ "$CHECK_ONLY" = 1 ]; then
  echo "Server is idle (currently $REMOTE_VERSION); no changes (--check)."
  print_addon_step
  exit 0
fi
echo "Server is idle (currently $REMOTE_VERSION); deploying $VERSION."

REMOTE="$REMOTE_USER@$IP"
STAMP="$$"
REMOTE_TMP="/tmp/ocr_server.py.$STAMP"
REMOTE_BACKUP="/tmp/ocr_server.py.previous.$STAMP"
SSH=(ssh -i "$SSH_KEY" -o BatchMode=yes -o ConnectTimeout=15 "$REMOTE")
SCP=(scp -i "$SSH_KEY" -o BatchMode=yes -o ConnectTimeout=15)

"${SCP[@]}" "$SERVER_SRC" "$REMOTE:$REMOTE_TMP"
# Compile before install. Keep a temporary backup on the VM until systemd reports active; on a failed restart restore it.
"${SSH[@]}" "set -eu
  python3 -m py_compile '$REMOTE_TMP'
  sudo cp /usr/local/bin/ocr_server.py '$REMOTE_BACKUP'
  sudo install -m 755 '$REMOTE_TMP' /usr/local/bin/ocr_server.py
  rm -f '$REMOTE_TMP'
  if ! sudo systemctl restart ocr || ! systemctl is-active --quiet ocr; then
    echo 'new server did not become active; restoring previous server' >&2
    sudo install -m 755 '$REMOTE_BACKUP' /usr/local/bin/ocr_server.py
    sudo systemctl restart ocr || true
    sudo rm -f '$REMOTE_BACKUP'
    exit 1
  fi
  sudo rm -f '$REMOTE_BACKUP'
"

STATUS="$(health)" || { echo "service restarted but public health check failed; inspect systemctl status ocr" >&2; exit 1; }
read -r BUSY QUEUED REMOTE_VERSION <<< "$(printf '%s' "$STATUS" | health_state)"
[ "$REMOTE_VERSION" = "$VERSION" ] || { echo "deployed server reports version '$REMOTE_VERSION', expected '$VERSION'" >&2; exit 1; }
[ "$BUSY" = false ] && [ "$QUEUED" = 0 ] || { echo "unexpected post-deploy health: $STATUS" >&2; exit 1; }
echo "Server update complete: $REMOTE_VERSION, idle."
print_addon_step
