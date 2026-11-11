#!/usr/bin/env bash
# Keeps a trusted TLS certificate for ORIGIN_HOST across server rebuilds.
#
#   cert_sync.sh ensure   (default) make /etc/livestack/tls/{fullchain,privkey}.pem valid
#   cert_sync.sh push     back up /etc/letsencrypt to R2
#
# WHY: the server is destroyed after every session. Issuing a fresh Let's Encrypt
# certificate on every boot would hit LE's limit of 5 duplicate certificates per
# week (we run 5 sessions/week). So the certificate is backed up to a private R2
# bucket and restored on boot; it is only re-issued when < CERT_MIN_DAYS remain.
#
# Fallback order (first that yields a valid cert wins):
#   1. /etc/letsencrypt already on disk (baked into the snapshot)
#   2. restore from R2   (STATE_BUCKET + STATE_R2_*)
#   3. certbot DNS-01 via Cloudflare  (CF_DNS_API_TOKEN) - works even before DNS/firewall is right
#   4. certbot HTTP-01 standalone     (needs port 80 reachable at ORIGIN_HOST)
#   5. self-signed (exit 1) - nginx still starts, but the CDN will refuse it
set -Eeuo pipefail
. "$(dirname "$(readlink -f "$0")")/common.sh"
: "${ORIGIN_HOST:?ORIGIN_HOST missing}"

MIN_DAYS=${CERT_MIN_DAYS:-21}
LE_LIVE=/etc/letsencrypt/live/$ORIGIN_HOST
REMOTE="r2state:${STATE_BUCKET:-}/certs/$ORIGIN_HOST/letsencrypt.tar.gz"

r2_configured() {
  [ -n "${STATE_BUCKET:-}" ] && [ -n "${STATE_R2_ACCOUNT_ID:-}" ] \
    && [ -n "${STATE_R2_ACCESS_KEY_ID:-}" ] && [ -n "${STATE_R2_SECRET_ACCESS_KEY:-}" ]
}
rclone_env() {
  export RCLONE_CONFIG_R2STATE_TYPE=s3 RCLONE_CONFIG_R2STATE_PROVIDER=Cloudflare \
    RCLONE_CONFIG_R2STATE_ACCESS_KEY_ID="$STATE_R2_ACCESS_KEY_ID" \
    RCLONE_CONFIG_R2STATE_SECRET_ACCESS_KEY="$STATE_R2_SECRET_ACCESS_KEY" \
    RCLONE_CONFIG_R2STATE_ENDPOINT="https://$STATE_R2_ACCOUNT_ID.r2.cloudflarestorage.com" \
    RCLONE_CONFIG_R2STATE_NO_CHECK_BUCKET=true
}
valid() {
  [ -s "$LE_LIVE/fullchain.pem" ] && [ -s "$LE_LIVE/privkey.pem" ] \
    && openssl x509 -checkend $((MIN_DAYS * 86400)) -noout -in "$LE_LIVE/fullchain.pem" >/dev/null 2>&1
}
link_le() {
  install -d -m 755 "$TLS_DIR"
  ln -sfn "$LE_LIVE/fullchain.pem" "$TLS_DIR/fullchain.pem"
  ln -sfn "$LE_LIVE/privkey.pem"   "$TLS_DIR/privkey.pem"
  log "using Let's Encrypt cert, expires $(openssl x509 -enddate -noout -in "$LE_LIVE/fullchain.pem" | cut -d= -f2)"
}
pull() {
  r2_configured || { log "R2 state bucket not configured - skipping restore"; return 1; }
  rclone_env
  local tmp; tmp=$(mktemp)
  if timeout 120 rclone copyto "$REMOTE" "$tmp" --retries 3 -q; then
    tar -xzf "$tmp" -C /
    rm -f "$tmp"
    log "restored /etc/letsencrypt from R2"
  else
    rm -f "$tmp"; log "no certificate backup in R2 (or R2 unreachable)"; return 1
  fi
}
push() {
  r2_configured || { log "R2 state bucket not configured - certificate NOT backed up"; return 0; }
  rclone_env
  local tmp; tmp=$(mktemp --suffix .tar.gz)
  tar -czf "$tmp" -C / etc/letsencrypt
  if timeout 120 rclone copyto "$tmp" "$REMOTE" --retries 3 -q; then log "certificate backed up to R2"
  else alert warning "certificate backup to R2 failed (next boot may need to re-issue)"; fi
  rm -f "$tmp"
}
issue_dns() {
  [ -n "${CF_DNS_API_TOKEN:-}" ] || return 1
  log "issuing via certbot DNS-01 (Cloudflare)"
  install -d -m 700 /root/.secrets
  ( umask 077; printf 'dns_cloudflare_api_token = %s\n' "$CF_DNS_API_TOKEN" > /root/.secrets/cloudflare.ini )
  certbot certonly --non-interactive --agree-tos -m "${ACME_EMAIL:?ACME_EMAIL missing}" \
    --dns-cloudflare --dns-cloudflare-credentials /root/.secrets/cloudflare.ini \
    --dns-cloudflare-propagation-seconds 30 \
    --cert-name "$ORIGIN_HOST" -d "$ORIGIN_HOST" --keep-until-expiring
}
issue_http() {
  [ -n "${ACME_EMAIL:-}" ] || return 1
  log "issuing via certbot HTTP-01 (standalone on :80)"
  systemctl stop nginx 2>/dev/null || true
  certbot certonly --non-interactive --agree-tos -m "$ACME_EMAIL" --standalone \
    --preferred-challenges http --cert-name "$ORIGIN_HOST" -d "$ORIGIN_HOST" --keep-until-expiring
}
self_signed() {
  install -d -m 755 "$TLS_DIR"
  openssl req -x509 -newkey rsa:2048 -nodes -days 7 -subj "/CN=$ORIGIN_HOST" \
    -keyout "$TLS_DIR/selfsigned.key" -out "$TLS_DIR/selfsigned.crt" >/dev/null 2>&1
  chmod 600 "$TLS_DIR/selfsigned.key"
  ln -sfn "$TLS_DIR/selfsigned.crt" "$TLS_DIR/fullchain.pem"
  ln -sfn "$TLS_DIR/selfsigned.key" "$TLS_DIR/privkey.pem"
  log "installed SELF-SIGNED fallback certificate"
}

case "${1:-ensure}" in
  push) push ;;
  ensure)
    if valid; then link_le; exit 0; fi
    pull || true
    if valid; then link_le; exit 0; fi
    if issue_dns || issue_http; then
      if valid; then link_le; push; exit 0; fi
    fi
    self_signed
    exit 1 ;;
  *) echo "usage: $0 [ensure|push]" >&2; exit 2 ;;
esac
