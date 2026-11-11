# shellcheck shell=bash
# Shared settings/helpers for the server scripts. Source it, don't run it.
LIVESTACK_HOME=${LIVESTACK_HOME:-/opt/livestack}
ENV_FILE=${ENV_FILE:-/etc/livestack/env}
# tr strips CR in case the file was written from Windows with CRLF endings.
if [ -r "$ENV_FILE" ]; then set -a; . <(tr -d '\r' < "$ENV_FILE"); set +a; fi

HLS_ROOT=${HLS_ROOT:-/var/www/hls}
RUN_DIR=${RUN_DIR:-/run/livestack}
REC_DIR=${REC_DIR:-/var/lib/livestack/recordings}
MTX_API=${MTX_API:-http://127.0.0.1:9997}
RTSP_BASE=${RTSP_BASE:-rtsp://127.0.0.1:8554}
TLS_DIR=${TLS_DIR:-/etc/livestack/tls}

log()   { printf '%s [%s] %s\n' "$(date -u +%FT%TZ)" "${0##*/}" "$*" >&2; }
die()   { log "FATAL: $*"; exit 1; }
alert() { "$LIVESTACK_HOME/server/alert.sh" "$@" || true; }

# True when the clock is NTP-synchronised (works with chrony or systemd-timesyncd).
ntp_synced() {
  [ "$(timedatectl show -p NTPSynchronized --value 2>/dev/null)" = yes ] && return 0
  command -v chronyc >/dev/null && chronyc tracking 2>/dev/null | grep -q 'Leap status *: Normal'
}
