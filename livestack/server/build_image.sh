#!/usr/bin/env bash
# ONE-TIME: turn a fresh Vultr Ubuntu 24.04 instance into the golden image.
#
#   1. copy this repo to the server and run:   bash server/build_image.sh
#   2. test a full session on it (push /etc/livestack/env, run boot.sh)
#   3. clean it for snapshotting:              bash /opt/livestack/server/build_image.sh --finalize
#   4. from your PC: python control/livestack.py snapshot --instance-id <id>
#
# See docs/SETUP.md. Re-run safely (idempotent).
set -Eeuo pipefail
MEDIAMTX_VERSION=${MEDIAMTX_VERSION:-v1.21.2}
LIVESTACK_HOME=/opt/livestack
SRC_DIR=$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)
log() { printf '\n==> %s\n' "$*"; }
[ "$(id -u)" -eq 0 ] || { echo "run as root" >&2; exit 1; }

if [ "${1:-}" = "--finalize" ]; then
  log "finalize: stop services and remove per-session data/secrets before snapshot"
  systemctl stop livestack-monitor mediamtx nginx 2>/dev/null || true
  systemctl disable livestack-monitor mediamtx nginx 2>/dev/null || true
  rm -f /etc/livestack/env /etc/livestack/mediamtx.yml
  rm -rf /root/.secrets /var/www/hls/* /var/lib/livestack/recordings/* /run/livestack/* /run/livestack-monitor
  # /etc/letsencrypt is KEPT: a valid cert in the snapshot is fallback #1 in cert_sync.sh.
  journalctl --rotate >/dev/null 2>&1 || true
  journalctl --vacuum-time=1s >/dev/null 2>&1 || true
  rm -f /root/.bash_history
  log "done - shut nothing down; snapshot now with: python control/livestack.py snapshot --instance-id <id>"
  exit 0
fi

export DEBIAN_FRONTEND=noninteractive
log "packages"
apt-get update -q
apt-get -y -q upgrade
apt-get install -y -q --no-install-recommends \
  ffmpeg nginx certbot python3-certbot-dns-cloudflare rclone ufw chrony jq curl git \
  gettext-base openssl ca-certificates tar procps iproute2 python3

log "MediaMTX $MEDIAMTX_VERSION"
case "$(uname -m)" in
  x86_64) arch=amd64 ;; aarch64) arch=arm64 ;; *) echo "unsupported arch" >&2; exit 1 ;;
esac
tmp=$(mktemp -d)
f="mediamtx_${MEDIAMTX_VERSION}_linux_${arch}.tar.gz"
base="https://github.com/bluenviron/mediamtx/releases/download/$MEDIAMTX_VERSION"
curl -fsSL -o "$tmp/$f" "$base/$f"
curl -fsSL -o "$tmp/checksums.sha256" "$base/checksums.sha256"
(cd "$tmp" && sha256sum --ignore-missing -c checksums.sha256)   # abort on checksum mismatch
tar -xzf "$tmp/$f" -C "$tmp" mediamtx
install -m 755 "$tmp/mediamtx" /usr/local/bin/mediamtx
rm -rf "$tmp"
/usr/local/bin/mediamtx --version

log "user and directories"
id livestack >/dev/null 2>&1 || useradd --system --home /var/lib/livestack --shell /usr/sbin/nologin livestack
install -d -o livestack -g livestack -m 755 /var/www/hls /var/lib/livestack /var/lib/livestack/recordings
install -d -m 755 /etc/livestack /etc/livestack/tls /var/www/acme

log "code -> $LIVESTACK_HOME"
if [ "$SRC_DIR" != "$LIVESTACK_HOME" ]; then
  if [ -n "${CONFIG_REPO_URL:-}" ]; then
    rm -rf "$LIVESTACK_HOME"; git clone --quiet "$CONFIG_REPO_URL" "$LIVESTACK_HOME"
  else
    mkdir -p "$LIVESTACK_HOME"; cp -a "$SRC_DIR/." "$LIVESTACK_HOME/"
  fi
fi
find "$LIVESTACK_HOME/server" -name '*.sh' -exec sed -i 's/\r$//' {} + -exec chmod 755 {} +   # strip CRLF from Windows checkouts
sed -i 's/\r$//' "$LIVESTACK_HOME"/config/*.tmpl "$LIVESTACK_HOME"/config/*.conf "$LIVESTACK_HOME"/config/systemd/*.service

log "systemd"
install -m 644 "$LIVESTACK_HOME"/config/systemd/*.service /etc/systemd/system/
mkdir -p /etc/systemd/system/nginx.service.d
printf '[Service]\nRestart=on-failure\nRestartSec=2\n' > /etc/systemd/system/nginx.service.d/restart.conf
mkdir -p /etc/systemd/journald.conf.d
printf '[Journal]\nSystemMaxUse=500M\n' > /etc/systemd/journald.conf.d/livestack.conf
systemctl daemon-reload
systemctl enable chrony livestack-boot.service >/dev/null
# Services are started by boot.sh once the session env exists.
systemctl disable --now nginx mediamtx livestack-monitor >/dev/null 2>&1 || true
rm -f /etc/nginx/sites-enabled/default

log "ssh hardening (key-only)"
mkdir -p /etc/ssh/sshd_config.d
printf 'PasswordAuthentication no\nKbdInteractiveAuthentication no\nPermitRootLogin prohibit-password\n' \
  > /etc/ssh/sshd_config.d/90-livestack.conf
systemctl reload ssh 2>/dev/null || systemctl reload sshd 2>/dev/null || true

log "unattended security upgrades off during sessions (avoid restarts mid-stream)"
systemctl disable --now unattended-upgrades apt-daily.timer apt-daily-upgrade.timer >/dev/null 2>&1 || true

log "image ready. Next: push an env file and run $LIVESTACK_HOME/server/boot.sh to test, then --finalize."
