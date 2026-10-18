#!/usr/bin/env bash
# Contract test for update.sh. It replaces curl, SSH and SCP with local fakes, so no VM or network is touched.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
BIN="$TMP/bin"
mkdir -p "$BIN" "$TMP/cache"

cat > "$BIN/curl" <<'EOF'
#!/usr/bin/env bash
cat "$MOCK_HEALTH"
EOF
cat > "$BIN/scp" <<'EOF'
#!/usr/bin/env bash
printf 'scp %s\n' "$*" >> "$MOCK_LOG"
cp "${@: -2:1}" "$MOCK_UPLOADED"
EOF
cat > "$BIN/ssh" <<'EOF'
#!/usr/bin/env bash
printf 'ssh %s\n' "$*" >> "$MOCK_LOG"
case "$*" in
  *'sudo rm -f'*) ;;
  *) echo 'update command must remove the root-owned backup with sudo' >&2; exit 1 ;;
esac
printf '%s\n' '{"ok":true,"version":"0.1.3","busy":false,"queued":0}' > "$MOCK_HEALTH"
EOF
chmod +x "$BIN"/*

# A local miniature release, so testing never uses the real cached host, key or source tree.
MINI="$TMP/release"
mkdir -p "$MINI/server" "$MINI/addon" "$MINI/deploy"
cp "$ROOT/deploy/update.sh" "$MINI/deploy/update.sh"
printf '0.1.3\n' > "$MINI/VERSION"
printf 'VERSION = "0.1.3"\n' > "$MINI/server/ocr_server.py"
printf "var OCR_VERSION = '0.1.3';\n" > "$MINI/addon/Code.gs"
printf '127.0.0.1\n' > "$TMP/cache/ip"
printf 'ocr.example.test\n' > "$TMP/cache/host"
printf 'private key placeholder\n' > "$TMP/key"
chmod 600 "$TMP/key"
printf '%s\n' '{"ok":true,"version":"0.1.2","busy":false,"queued":0}' > "$TMP/health"
: > "$TMP/log"

run() {
  PATH="$BIN:$PATH" \
  OCR_IP_FILE="$TMP/cache/ip" OCR_HOST_FILE="$TMP/cache/host" OCR_SSH_KEY="$TMP/key" OCR_REMOTE_USER="test" \
  MOCK_HEALTH="$TMP/health" MOCK_LOG="$TMP/log" MOCK_UPLOADED="$TMP/uploaded" \
  "$MINI/deploy/update.sh" "$@"
}

bash -n "$ROOT/deploy/update.sh"
OUTPUT="$(run --dry-run)"
grep -Fq 'Dry run: local release 0.1.3 is consistent.' <<< "$OUTPUT"
[ ! -s "$TMP/log" ]  # dry run does not talk to the fakes
OUTPUT="$(run --check)"
grep -Fq 'Server is idle (currently 0.1.2); no changes (--check).' <<< "$OUTPUT"
[ ! -s "$TMP/log" ]  # check makes no SSH or SCP call
OUTPUT="$(run)"
grep -Fq 'Server update complete: 0.1.3, idle.' <<< "$OUTPUT"
grep -q '^scp ' "$TMP/log"
grep -q '^ssh ' "$TMP/log"
cmp "$MINI/server/ocr_server.py" "$TMP/uploaded"

: > "$TMP/log"
printf '%s\n' '{"ok":true,"version":"0.1.3","busy":true,"queued":1}' > "$TMP/health"
if run >/dev/null 2>&1; then
  echo 'busy server should have refused deployment' >&2
  exit 1
fi
[ ! -s "$TMP/log" ]  # refusal happens before SCP or SSH

: > "$TMP/log"
printf "var OCR_VERSION = '0.1.2';\n" > "$MINI/addon/Code.gs"
if run --dry-run >/dev/null 2>&1; then
  echo 'mismatched add-on version should have refused deployment' >&2
  exit 1
fi
[ ! -s "$TMP/log" ]  # version validation happens before any network action
printf 'update.sh contract: dry run, check, deploy, busy refusal and version guard passed\n'
