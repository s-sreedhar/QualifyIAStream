#!/usr/bin/env bash
# livestack watchdog - runs for the whole session (systemd: livestack-monitor).
#
# Every MONITOR_INTERVAL seconds:
#   services   mediamtx / nginx active          -> alert + restart
#   streams    for each live path, the ladder's 720p playlist must have been
#              rewritten within STALE_SECONDS   -> alert + kill the hung FFmpeg
#                                                  (MediaMTX restarts it)
#   speed      ladder speed >= MIN_SPEED for SPEED_STRIKES checks in a row
#   disk       usage < DISK_MAX_PCT
#   ntp        clock synchronised (every 60 s)
# Alerts are de-duplicated: sent once, repeated every RENOTIFY_SECONDS while
# active, and a RESOLVED message is sent on recovery.
set -uo pipefail
. "$(dirname "$(readlink -f "$0")")/common.sh"

INTERVAL=${MONITOR_INTERVAL:-10}
STALE_SECONDS=${STALE_SECONDS:-20}
STARTUP_GRACE=${STARTUP_GRACE:-30}
MIN_SPEED=${MIN_SPEED:-0.97}
SPEED_STRIKES=${SPEED_STRIKES:-3}
DISK_MAX_PCT=${DISK_MAX_PCT:-85}
RENOTIFY=${RENOTIFY_SECONDS:-300}
STATE=/run/livestack-monitor
mkdir -p "$STATE"
declare -A first_ready=() speed_bad=()

raise() {  # raise KEY LEVEL MESSAGE
  local f="$STATE/$1" now; now=$(date +%s)
  if [ -f "$f" ] && [ $((now - $(cat "$f"))) -lt "$RENOTIFY" ]; then return 0; fi
  echo "$now" >"$f"; alert "$2" "$3"
}
resolve() {  # resolve KEY MESSAGE
  local f="$STATE/$1"
  if [ -f "$f" ]; then rm -f "$f"; alert info "RESOLVED: $2"; fi
}

check_services() {
  for s in mediamtx nginx; do
    if systemctl is-active --quiet "$s"; then resolve "svc_$s" "$s running"
    else raise "svc_$s" error "$s is not running - restarting"; systemctl restart "$s" || true; fi
  done
}

check_disk() {
  local pct; pct=$(df --output=pcent "$HLS_ROOT" | tail -1 | tr -dc '0-9')
  if [ "${pct:-0}" -ge "$DISK_MAX_PCT" ]; then raise disk error "disk ${pct}% full on $HLS_ROOT"
  else resolve disk "disk usage back to ${pct}%"; fi
}

check_ntp() {
  if ntp_synced; then resolve ntp "clock synchronised"; else raise ntp warning "clock not NTP-synchronised"; fi
}

check_streams() {
  local now paths p key pl age prog sp
  now=$(date +%s)
  if ! paths=$(curl -fsS -m 5 "$MTX_API/v3/paths/list?itemsPerPage=1000" | jq -r '.items[] | select(.ready) | .name'); then
    raise api error "MediaMTX API unreachable"; return
  fi
  resolve api "MediaMTX API reachable"

  declare -A seen=()
  for p in $paths; do
    [[ "$p" == healthcheck/* ]] && continue
    seen[$p]=1
    if [ -z "${first_ready[$p]:-}" ]; then first_ready[$p]=$now; log "stream live: $p"; alert info "stream live: $p"; fi
    [ $((now - first_ready[$p])) -lt "$STARTUP_GRACE" ] && continue

    key="stale_${p//\//_}"
    pl="$HLS_ROOT/$p/720p/index.m3u8"
    if [ -f "$pl" ]; then age=$((now - $(stat -c %Y "$pl"))); else age=9999; fi
    if [ "$age" -gt "$STALE_SECONDS" ]; then
      raise "$key" error "ladder output for $p stale (${age}s) - restarting encoder"
      pkill -TERM -f -- "${RTSP_BASE}/${p}( |\$)" || true   # MediaMTX restarts it
      first_ready[$p]=$now                                    # fresh grace period
      continue
    fi
    resolve "$key" "ladder output for $p fresh again"

    prog="$RUN_DIR/progress-${p//\//_}"
    sp=$(tail -n 40 "$prog" 2>/dev/null | grep '^speed=' | tail -1 | tr -dc '0-9.')
    [[ "$sp" =~ ^[0-9]+(\.[0-9]+)?$ ]] || continue
    if awk -v s="$sp" -v m="$MIN_SPEED" 'BEGIN{exit !(s < m)}'; then
      speed_bad[$p]=$(( ${speed_bad[$p]:-0} + 1 ))
      [ "${speed_bad[$p]}" -ge "$SPEED_STRIKES" ] && \
        raise "speed_${p//\//_}" error "encoder for $p at ${sp}x (< ${MIN_SPEED}x) - CPU overloaded; viewers will buffer"
    else
      speed_bad[$p]=0
      resolve "speed_${p//\//_}" "encoder for $p back to ${sp}x"
    fi
  done

  for p in "${!first_ready[@]}"; do
    if [ -z "${seen[$p]:-}" ]; then
      unset "first_ready[$p]" "speed_bad[$p]"
      log "stream ended: $p"
      alert warning "stream ended: $p (publisher disconnected - expected only at the end of the session)"
    fi
  done
}

log "monitor started (interval ${INTERVAL}s, stale after ${STALE_SECONDS}s)"
i=0
while true; do
  check_services
  check_disk
  check_streams
  [ $((i % 6)) -eq 0 ] && check_ntp
  date +%s >"$STATE/heartbeat"
  i=$((i + 1))
  sleep "$INTERVAL"
done
