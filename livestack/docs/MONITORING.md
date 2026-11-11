# Monitoring and alerts

Three layers, each catching what the others can't see:

| Layer | Runs | Watches | Catches |
|---|---|---|---|
| `server/healthcheck.sh` | once at boot (and `livestack.py health`) | the whole chain using a test pattern | broken image, DNS, TLS, firewall, CPU too small, before anyone goes live |
| `server/monitor.sh` | on the VPS, every 10 s (systemd) | MediaMTX API, ladder output files, FFmpeg progress | hung or slow encoder (self-heals), crashed services, disk, NTP |
| `control/probe.py` | on your PC (or any other machine) | the **CDN URL**, exactly like a viewer | CDN or DNS problems, origin unreachable, anything the server can't see about itself |

All three post to `ALERT_WEBHOOK_URL` (Discord, Slack or ntfy). They de-duplicate:
- an alert is sent once,
- it is repeated every 5 min while still true,
- `RESOLVED: …` is sent when it clears.

## Alert reference

| Alert | Source | Meaning / action |
|---|---|---|
| `ladder output for live/x stale (Ns) - restarting encoder` | monitor | FFmpeg stopped producing. It is killed and MediaMTX restarts it. Expect RESOLVED within ~30 s. If it repeats, check `journalctl -u mediamtx`. |
| `encoder for live/x at 0.9x` | monitor | CPU can't keep realtime. Viewers will buffer. See RUNBOOK. |
| `mediamtx/nginx is not running - restarting` | monitor | Service died; it is restarted automatically. |
| `MediaMTX API unreachable` | monitor | MediaMTX hung or restarting. |
| `disk N% full` | monitor | Should not happen (segments are deleted as they age out). Check `/var/lib/livestack/recordings`. |
| `clock not NTP-synchronised` | monitor | Players' program-date-time drifts. Usually transient. |
| `stream live: live/x` / `stream ended: live/x` | monitor | Informational. "ended" outside the planned end time means OBS disconnected. |
| `variant 720p stale` / `master playlist unreachable` | probe | What viewers experience. Compare against the origin URL to tell a CDN problem from an origin problem. |
| `health check failed: …` | healthcheck | Boot is not ready. `start` prints NOT READY. |
| `boot.sh failed at line N` | boot | See `start`'s output. Re-run `start` after fixing. |
| `TLS: … self-signed fallback` | boot | The CDN will fail over HTTPS. Fix tokens and re-run `start`. |
| `stop refused … replay not verified` | stop | Upload the replay first. |
| `N livestack server(s) running longer than 4 h` | orphans | Forgotten server, still billing. |

## Encoder speed

FFmpeg reports `speed=1.00x` when it keeps up with realtime. Because the input is live, it can't go above about 1.0x for long. The local test reached 1.15x only while catching up at startup.
Below 1.0x, segments arrive late and players stall. The monitor alerts below 0.97x for 3 checks in a row (30 s).
The current value is in `/run/livestack/progress-live_<name>` (`speed=` line).

## Useful commands on the server

```bash
journalctl -u mediamtx -f                    # MediaMTX + ladder (FFmpeg warnings)
journalctl -u livestack-monitor -f           # watchdog decisions
journalctl -t livestack-alert                # every alert sent
cat /run/livestack/health.json | jq          # last health check
grep speed= /run/livestack/progress-live_test | tail -1
curl -s localhost:9997/v3/paths/list | jq    # live paths, readers, bytes
curl -s localhost:9998/metrics | head        # Prometheus metrics
ls -la /var/www/hls/live/test/720p/          # segments being written
```

## Budget alerts (outside this repo)

- **Vultr:** Billing → set a monthly threshold alert, for example $15.
- **Cloudflare R2:** Billing → Notifications → usage-based billing threshold.
- **BlazingCDN:** traffic alert near your plan's TB.
- **Orphan check:** scheduled task (SETUP §9).
