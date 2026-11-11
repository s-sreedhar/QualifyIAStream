#!/usr/bin/env bash
# Brings a server restored from the golden snapshot to a streaming-ready state.
# Idempotent: safe to re-run at any time (start_session re-runs it on resume,
# livestack-boot.service runs it after an unexpected reboot).
#
#   boot.sh                   full boot + health check (exit 0 = ready to go live)
#   boot.sh --no-healthcheck  skip the test-pattern health check
#
# Steps: validate env -> reserved IP -> sync config repo -> time sync ->
#        render configs -> firewall -> TLS -> start services -> health check
set -Eeuo pipefail
SELF=$(readlink -f "$0")
. "$(dirname "$SELF")/common.sh"
[ "$(id -u)" -eq 0 ] || die "run as root"
trap 'alert error "boot.sh failed at line $LINENO (exit $?)"' ERR

SKIP_HEALTH=0
[ "${1:-}" = "--no-healthcheck" ] && SKIP_HEALTH=1
step() { log "==> $*"; }

# --------------------------------------------------------------------------- 1
step "validate settings ($ENV_FILE)"
[ -r "$ENV_FILE" ] || die "$ENV_FILE missing (start_session pushes it)"
for v in ORIGIN_HOST PUBLISH_USER PUBLISH_PASS STREAMER_IPS; do
  [ -n "${!v:-}" ] || die "$v is not set in $ENV_FILE"
done
[[ "$ORIGIN_HOST" =~ ^[A-Za-z0-9.-]+$ ]]          || die "ORIGIN_HOST is not a valid hostname"
[[ "$PUBLISH_USER" =~ ^[A-Za-z0-9_-]{3,64}$ ]]    || die "PUBLISH_USER must be 3-64 chars of [A-Za-z0-9_-]"
[[ "$PUBLISH_PASS" =~ ^[A-Za-z0-9_-]{16,128}$ ]]  || die "PUBLISH_PASS must be 16-128 chars of [A-Za-z0-9_-]"

# --------------------------------------------------------------------------- 2
step "reserved IP"
if [ -n "${RESERVED_IP:-}" ] && [ "${CONFIGURE_RESERVED_IP:-auto}" != "no" ]; then
  if ip -4 -o addr show | grep -qw -- "$RESERVED_IP"; then
    log "reserved IP $RESERVED_IP already on an interface"
  else
    iface=$(ip -4 route show default | awk '{print $5; exit}')
    ip addr add "$RESERVED_IP/32" dev "$iface"
    log "added $RESERVED_IP/32 to $iface (runtime; the server is rebuilt every session)"
  fi
fi

# --------------------------------------------------------------------------- 3
step "sync config repo"
if [ -n "${CONFIG_REPO_URL:-}" ] && [ -d "$LIVESTACK_HOME/.git" ] && [ -z "${LIVESTACK_SYNCED:-}" ]; then
  ref=${CONFIG_REPO_REF:-main}
  before=$(git -C "$LIVESTACK_HOME" rev-parse HEAD 2>/dev/null || echo none)
  if timeout 60 git -C "$LIVESTACK_HOME" fetch --quiet origin "$ref" \
     && git -C "$LIVESTACK_HOME" -c advice.detachedHead=false checkout --quiet --force FETCH_HEAD; then
    after=$(git -C "$LIVESTACK_HOME" rev-parse HEAD)
    chmod +x "$LIVESTACK_HOME"/server/*.sh
    if [ "$before" != "$after" ]; then
      log "config updated ${before:0:8} -> ${after:0:8}; re-executing new boot.sh"
      export LIVESTACK_SYNCED=1
      exec "$SELF" "$@"
    fi
    log "config up to date at ${after:0:8}"
  else
    alert warning "config repo sync failed - continuing with the config baked into the snapshot (${before:0:8})"
  fi
else
  log "skipped (CONFIG_REPO_URL not set or already synced)"
fi

# --------------------------------------------------------------------------- 4
step "time sync"
timedatectl set-ntp true 2>/dev/null || true
command -v chronyc >/dev/null && chronyc -a makestep >/dev/null 2>&1 || true
for _ in $(seq 1 30); do ntp_synced && break; sleep 2; done
ntp_synced && log "clock synchronised" || alert warning "NTP not yet synchronised (HLS program-date-time may drift)"

# --------------------------------------------------------------------------- 5
step "render configs"
install -d -o livestack -g livestack -m 755 "$HLS_ROOT" "$REC_DIR"
install -d -m 755 /etc/livestack "$TLS_DIR" /var/www/acme

tmp=$(mktemp)
PUBLISH_USER=$PUBLISH_USER PUBLISH_PASS=$PUBLISH_PASS \
  envsubst '${PUBLISH_USER} ${PUBLISH_PASS}' < "$LIVESTACK_HOME/config/mediamtx.yml.tmpl" > "$tmp"
if [ "${RECORD_BACKUP:-yes}" != "yes" ]; then
  sed -i 's/^\(\s*record:\) yes/\1 no/' "$tmp"
  log "server-side backup recording disabled (RECORD_BACKUP=${RECORD_BACKUP})"
fi
install -m 640 -o root -g livestack "$tmp" /etc/livestack/mediamtx.yml
rm -f "$tmp"

sed "s/__ORIGIN_HOST__/$ORIGIN_HOST/g" "$LIVESTACK_HOME/config/nginx-origin.conf" \
  > /etc/nginx/sites-available/livestack-origin.conf
ln -sf /etc/nginx/sites-available/livestack-origin.conf /etc/nginx/sites-enabled/livestack-origin.conf
rm -f /etc/nginx/sites-enabled/default

install -m 644 "$LIVESTACK_HOME"/config/systemd/*.service /etc/systemd/system/
systemctl daemon-reload

# --------------------------------------------------------------------------- 6
step "firewall"
ufw --force reset >/dev/null
ufw default deny incoming >/dev/null
ufw default allow outgoing >/dev/null
if [ -n "${ADMIN_IPS:-}" ]; then
  IFS=',' read -ra admins <<<"$ADMIN_IPS"
  for ip in "${admins[@]}"; do ufw allow from "${ip// /}" to any port 22 proto tcp >/dev/null; done
else
  ufw allow 22/tcp >/dev/null        # key-only auth (PasswordAuthentication no in the image)
fi
ufw allow 80/tcp >/dev/null          # ACME + redirect
ufw allow 443/tcp >/dev/null         # CDN pulls the origin over HTTPS
if [ "$STREAMER_IPS" = "any" ]; then
  ufw allow 1935/tcp >/dev/null
  log "RTMP open to any IP (publish still requires PUBLISH_USER/PUBLISH_PASS)"
else
  IFS=',' read -ra streamers <<<"$STREAMER_IPS"
  for ip in "${streamers[@]}"; do ufw allow from "${ip// /}" to any port 1935 proto tcp >/dev/null; done
  log "RTMP allowed from: $STREAMER_IPS"
fi
ufw --force enable >/dev/null

# --------------------------------------------------------------------------- 7
step "TLS certificate"
if ! "$LIVESTACK_HOME/server/cert_sync.sh" ensure; then
  alert error "TLS: no trusted certificate for $ORIGIN_HOST - serving a self-signed fallback; CDN HTTPS pulls WILL fail"
fi

# --------------------------------------------------------------------------- 8
step "start services"
nginx -t
systemctl enable nginx >/dev/null 2>&1
systemctl reload-or-restart nginx
systemctl enable mediamtx livestack-monitor >/dev/null 2>&1
systemctl restart mediamtx
for _ in $(seq 1 10); do curl -fsS -m 2 "$MTX_API/v3/paths/list" >/dev/null 2>&1 && break; sleep 1; done
systemctl is-active --quiet mediamtx || die "mediamtx failed to start: $(journalctl -u mediamtx -n 20 --no-pager)"
systemctl restart livestack-monitor

# --------------------------------------------------------------------------- 9
if [ "$SKIP_HEALTH" -eq 0 ]; then
  step "health check"
  "$LIVESTACK_HOME/server/healthcheck.sh"
fi
log "boot complete"
