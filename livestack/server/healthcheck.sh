#!/usr/bin/env bash
# Pre-flight health check. ALL checks must pass before the streamer goes live.
# Exit 0 = ready. Results also written to /run/livestack/health.json.
#
#  service:mediamtx, service:nginx   running
#  rtmp_port                         1935 accepts a TCP connection
#  ladder                            a test pattern pushed over RTMP produces
#                                    master.m3u8 with 3 variants, each with segments
#  origin_https                      that playlist is served at https://ORIGIN_HOST/hls/...
#                                    with a trusted certificate and the HLS MIME type
#  tls_cert                          trusted (not the self-signed fallback), >= 14 days left
#  encoder_speed                     ladder runs at >= 0.95x realtime
#  ntp                               clock synchronised
#  disk                              < 80% used
set -uo pipefail
. "$(dirname "$(readlink -f "$0")")/common.sh"

PROBE_PATH=healthcheck/probe
TIMEOUT=${HEALTH_TIMEOUT:-60}
out="$HLS_ROOT/$PROBE_PATH"
mkdir -p "$RUN_DIR"
results=(); fails=0
pass() { results+=("PASS|$1|$2"); printf '  PASS  %-16s %s\n' "$1" "$2"; }
fail() { results+=("FAIL|$1|$2"); printf '  FAIL  %-16s %s\n' "$1" "$2"; fails=$((fails + 1)); }

echo "livestack health check ($(date -u +%FT%TZ), host $(hostname))"

# 1. services
for s in mediamtx nginx; do
  st=$(systemctl is-active "$s" 2>/dev/null); [ "$st" = active ] && pass "service:$s" running || fail "service:$s" "$st"
done

# 2. RTMP port
if timeout 3 bash -c '</dev/tcp/127.0.0.1/1935' 2>/dev/null; then pass rtmp_port "1935 accepting"
else fail rtmp_port "1935 not accepting connections"; fi

# 3. ladder with a test pattern
rm -rf "$out"
ffmpeg -hide_banner -loglevel error -re \
  -f lavfi -i "testsrc2=size=1280x720:rate=30" -f lavfi -i "sine=frequency=1000:sample_rate=48000" \
  -c:v libx264 -preset ultrafast -tune zerolatency -g 60 -pix_fmt yuv420p -c:a aac -b:a 64k \
  -t $((TIMEOUT + 30)) -f flv "rtmp://127.0.0.1:1935/$PROBE_PATH" \
  </dev/null >/dev/null 2>"$RUN_DIR/healthcheck-push.log" &
push_pid=$!
cleanup() { kill "$push_pid" 2>/dev/null; wait "$push_pid" 2>/dev/null; rm -rf "$out"; }
trap cleanup EXIT

ok=0; t=0
for t in $(seq 1 "$TIMEOUT"); do
  if [ -f "$out/master.m3u8" ] && [ "$(grep -c '^#EXT-X-STREAM-INF' "$out/master.m3u8")" -eq 3 ]; then
    n=0
    for v in 720p 480p 360p; do grep -qs '\.ts$' "$out/$v/index.m3u8" && n=$((n + 1)); done
    [ "$n" -eq 3 ] && { ok=1; break; }
  fi
  sleep 1
done
if [ "$ok" -eq 1 ]; then pass ladder "master.m3u8 with 3 variants after ${t}s"
else fail ladder "no complete 3-variant output within ${TIMEOUT}s (see journalctl -u mediamtx)"; fi

# 4. origin over HTTPS through the public hostname (DNS + firewall + nginx + cert)
if [ "$ok" -eq 1 ]; then
  resp=$(curl -sS -o /dev/null -m 10 -w '%{http_code} %{content_type}' \
         "https://$ORIGIN_HOST/hls/$PROBE_PATH/master.m3u8" 2>&1)
  case "$resp" in
    "200 application/vnd.apple.mpegurl"*) pass origin_https "https://$ORIGIN_HOST/hls/... -> $resp" ;;
    *) fail origin_https "https://$ORIGIN_HOST/hls/$PROBE_PATH/master.m3u8 -> $resp" ;;
  esac
else
  fail origin_https "skipped (ladder failed)"
fi

# 5. certificate
if [ "$(readlink -f "$TLS_DIR/fullchain.pem")" = "$(readlink -f "$TLS_DIR/selfsigned.crt" 2>/dev/null)" ]; then
  fail tls_cert "self-signed fallback in use"
elif openssl x509 -checkend $((14 * 86400)) -noout -in "$TLS_DIR/fullchain.pem" >/dev/null 2>&1; then
  pass tls_cert "valid until $(openssl x509 -enddate -noout -in "$TLS_DIR/fullchain.pem" | cut -d= -f2)"
else
  fail tls_cert "missing or expires within 14 days"
fi

# 6. encoder speed of the ladder that processed the test pattern
sp=$(grep '^speed=' "$RUN_DIR/progress-healthcheck_probe" 2>/dev/null | tail -1 | tr -dc '0-9.')
if [ -n "$sp" ] && awk -v s="$sp" 'BEGIN{exit !(s >= 0.95)}'; then pass encoder_speed "${sp}x"
else fail encoder_speed "${sp:-unknown}x (needs >= 1.0x; CPU too small?)"; fi

# 7. NTP
ntp_synced && pass ntp "synchronised" || fail ntp "not synchronised"

# 8. disk
pct=$(df --output=pcent "$HLS_ROOT" | tail -1 | tr -dc '0-9')
[ "${pct:-100}" -lt 80 ] && pass disk "${pct}% used" || fail disk "${pct}% used"

# summary + json
printf '%s\n' "${results[@]}" | jq -Rn --arg ts "$(date -u +%FT%TZ)" --argjson fails "$fails" \
  '{time:$ts, ok:($fails==0), checks:[inputs | split("|") | {result:.[0], check:.[1], detail:.[2]}]}' \
  > "$RUN_DIR/health.json" 2>/dev/null || true
if [ "$fails" -eq 0 ]; then echo "RESULT: READY - all checks passed"; exit 0; fi
echo "RESULT: NOT READY - $fails check(s) failed"
alert error "health check failed: $fails check(s) - $(printf '%s; ' "${results[@]}" | grep -o 'FAIL|[^;]*' | tr '\n' ' ')"
exit 1
