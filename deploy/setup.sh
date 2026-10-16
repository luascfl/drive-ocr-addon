#!/usr/bin/env bash
# Sets up (or updates) the OCR server on a fresh Ubuntu 24.04 VM. Safe to run again: it keeps an existing
# OCR_TOKEN, rewrites the unit and the Caddyfile, and restarts the service.
#
# usage:  sudo OCR_ALLOWED_AUD=<oauth client id of the add-on> ./deploy/setup.sh
# optional environment:
#   OCR_HOST            public host name for HTTPS (default: <public-ip-with-dashes>.nip.io, found through the
#                       instance metadata). Caddy gets a Let's Encrypt certificate for it on the first request.
#   OCR_MAX_PAGES       page limit per file (default 60; the smallest Always Free VM does about 15 s per page)
#   OCR_USER_ACTIVE_MAX, OCR_USER_DAILY_MAX, OCR_QUEUE_MAX   usage limits (defaults 2, 30, 5)
#   SETUP_SKIP_SYSTEMD=1  install files and packages only (used to test inside a container)
#   SETUP_SKIP_CADDY=1    do not install or configure Caddy (you bring your own HTTPS proxy)
set -euo pipefail

[ "$(id -u)" -eq 0 ] || { echo "run as root (sudo)"; exit 1; }
: "${OCR_ALLOWED_AUD:?set OCR_ALLOWED_AUD to the OAuth client id of the add-on (the aud of its tokens)}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVER_SRC="$HERE/../server/ocr_server.py"
[ -f "$SERVER_SRC" ] || { echo "missing $SERVER_SRC"; exit 1; }
export DEBIAN_FRONTEND=noninteractive

echo "== packages"
apt-get update -qq
apt-get install -y -qq tesseract-ocr tesseract-ocr-por tesseract-ocr-eng tesseract-ocr-ara tesseract-ocr-heb \
  tesseract-ocr-osd poppler-utils qpdf python3 python3-pypdf curl ca-certificates gnupg >/dev/null

echo "== swap (the smallest Always Free VM has 1 GB of RAM)"
if ! swapon --show --noheadings | grep -q .; then
  if [ ! -f /swapfile ]; then fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile >/dev/null; fi
  swapon /swapfile 2>/dev/null || echo "  could not enable swap here (container?), continuing"
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

echo "== server and configuration"
install -m 755 "$SERVER_SRC" /usr/local/bin/ocr_server.py
mkdir -p /etc/ocr
umask 077
TOKEN="$(grep -s '^OCR_TOKEN=' /etc/ocr/env | cut -d= -f2- || true)"
[ -n "$TOKEN" ] || TOKEN="$(head -c 32 /dev/urandom | base64 | tr -d '/+=\n' | head -c 40)"
printf 'OCR_TOKEN=%s\nOCR_ALLOWED_AUD=%s\n' "$TOKEN" "$OCR_ALLOWED_AUD" > /etc/ocr/env
chmod 600 /etc/ocr/env
umask 022

cat > /etc/systemd/system/ocr.service <<EOF
[Unit]
Description=OCR server
After=network.target
[Service]
EnvironmentFile=/etc/ocr/env
Environment=OCR_PORT=8080
Environment=OCR_MAX_PAGES=${OCR_MAX_PAGES:-60}
Environment=OCR_QUEUE_MAX=${OCR_QUEUE_MAX:-5}
Environment=OCR_USER_ACTIVE_MAX=${OCR_USER_ACTIVE_MAX:-2}
Environment=OCR_USER_DAILY_MAX=${OCR_USER_DAILY_MAX:-30}
ExecStart=/usr/bin/python3 /usr/local/bin/ocr_server.py
DynamicUser=yes
PrivateTmp=yes
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
Restart=on-failure
[Install]
WantedBy=multi-user.target
EOF

echo "== journal retention (14 days, no document content is ever logged)"
mkdir -p /etc/systemd/journald.conf.d
printf '[Journal]\nMaxRetentionSec=14day\nSystemMaxUse=200M\n' > /etc/systemd/journald.conf.d/retention.conf

if [ "${SETUP_SKIP_CADDY:-0}" != "1" ]; then
  echo "== caddy (HTTPS)"
  HOST="${OCR_HOST:-}"
  if [ -z "$HOST" ]; then
    IP="$(curl -fsS -m 5 -H 'Authorization: Bearer Oracle' http://169.254.169.254/opc/v2/vnics/ 2>/dev/null \
         | python3 -c 'import json,sys; print(json.load(sys.stdin)[0]["publicIp"])' 2>/dev/null || true)"
    [ -n "$IP" ] || IP="$(curl -fsS -m 8 https://api.ipify.org || true)"
    [ -n "$IP" ] || { echo "could not find the public IP: set OCR_HOST"; exit 1; }
    HOST="${IP//./-}.nip.io"
  fi
  if ! command -v caddy >/dev/null; then
    curl -fsSL https://dl.cloudsmith.io/public/caddy/stable/gpg.key | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
    curl -fsSL https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt \
      | sed 's#^deb #deb [signed-by=/usr/share/keyrings/caddy-stable-archive-keyring.gpg] #' > /etc/apt/sources.list.d/caddy.list
    apt-get update -qq && apt-get install -y -qq caddy >/dev/null
  fi
  cat > /etc/caddy/Caddyfile <<EOF
$HOST {
  request_body {
    max_size 52MB
  }
  reverse_proxy 127.0.0.1:8080 {
    transport http {
      response_header_timeout 300s
      read_timeout 300s
    }
  }
}
EOF
  echo "  host: $HOST"
fi

echo "== firewall inside the VM (ports 80 and 443; the cloud security list must allow them too)"
if command -v iptables >/dev/null && iptables -S INPUT >/dev/null 2>&1; then
  for port in 443 80; do
    iptables -C INPUT -p tcp --dport "$port" -j ACCEPT 2>/dev/null || iptables -I INPUT 1 -p tcp --dport "$port" -j ACCEPT
  done
  command -v netfilter-persistent >/dev/null && netfilter-persistent save >/dev/null 2>&1 || true
fi

if [ "${SETUP_SKIP_SYSTEMD:-0}" = "1" ]; then
  echo "== done (systemd skipped)"; exit 0
fi

echo "== start"
systemctl daemon-reload
systemctl restart systemd-journald
systemctl enable --now ocr >/dev/null
systemctl restart ocr
[ "${SETUP_SKIP_CADDY:-0}" = "1" ] || { systemctl enable caddy >/dev/null; systemctl restart caddy; }
sleep 2
systemctl is-active ocr
curl -fsS -m 5 http://127.0.0.1:8080/health && echo
echo "Done. Remember: the cloud firewall (security list) must allow TCP 80 and 443, and the add-on's OCR_URL must be https://${HOST:-<your host>}."
