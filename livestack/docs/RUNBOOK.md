# Session runbook

All commands run from `E:\Freelancing\QualifyIAStream\livestack`. Pick one session id per stream, for example `20261111-1900`.

## Before the stream (start about 15 min early)

```powershell
.\start_session.ps1 --session 20261111-1900
```
1. Wait for `READY - the streamer can go live.` This usually takes 3–6 minutes. Every health check must say PASS.
2. Configure OBS: *Settings → Stream → Custom*
   - Server: `rtmp://ingest.example.com/live`
   - Stream key: `test?user=<PUBLISH_USER>&pass=<PUBLISH_PASS>` (the name before `?` becomes the URL path)
3. Use these OBS output settings: x264 or NVENC, **30 fps**, keyframe interval **2 s**, CBR about 4500–6000 kbps, AAC audio.
   Enable *Advanced → Automatically reconnect*. Also enable local recording: *Settings → Output → Recording*, MKV format, which survives OBS crashes.
4. Start streaming, then start recording.
5. In a second window, start the viewer-side probe and leave it running:
   ```powershell
   python control\probe.py https://live.example.com/hls/live/test/master.m3u8
   ```
6. Open `local\player.html?src=https://live.example.com/hls/live/test/master.m3u8` and confirm picture and sound.

## During the stream

You'll get webhook alerts for: stale variants, unreachable playlists, encoder speed below realtime, service restarts, disk, and NTP. Each alert has a `RESOLVED` follow-up when it clears. See [MONITORING.md](MONITORING.md).
Use `python control\livestack.py health` for a full re-check. It pushes a 1-minute test pattern, which viewers do not see.

## After the stream

```powershell
# 1. stop streaming + recording in OBS
# 2. encode + upload + verify the replay (safe to re-run if interrupted)
python control\replay.py run "D:\OBS\2026-11-11 19-00-00.mkv" --session-id 20261111-1900
# 3. destroy the server (refuses unless step 2 verified)
.\stop_session.ps1
```
To give the LMS a link: `python control\replay.py presign --key replays/2026/11/20261111-1900.mp4`. It is valid for 1 h. Normally the LMS backend generates these itself.

## Problems

### `start` stopped or failed halfway
Run the same command again. It resumes from `state\session.json`.
The server **keeps billing** until it is destroyed. To abandon it: `.\stop_session.ps1 --skip-replay-check`.

### Health check FAIL lines
| Check | Usual cause | Fix |
|---|---|---|
| `origin_https` | DNS for `origin` not at the reserved IP, or Cloudflare proxy (orange cloud) on | Fix DNS, re-run `start`. |
| `tls_cert` self-signed | `CF_DNS_API_TOKEN` wrong, R2 state creds wrong, or ACME rate limit | Check `journalctl -t livestack-alert`, fix `.env`, re-run `start`. |
| `ladder` / `encoder_speed` | Plan too small, or the FFmpeg build is missing libx264 | `ssh root@<ip> journalctl -u mediamtx -n 100`. |
| `ntp` | Time not synced yet | Re-run `livestack.py health` after a minute. |

### Streamer cannot connect (OBS "Failed to connect")
1. The home IP probably changed. Check it at https://ifconfig.me, update `STREAMER_IPS` in `.env`, and run `.\start_session.ps1` again (about 1 minute; it re-applies the firewall).
   If this happens often, set `STREAMER_IPS=any`. Publish credentials still protect ingest.
2. Wrong key: the server logs `authentication failed` (`journalctl -u mediamtx | grep auth`).

### Viewers buffer / probe says "stale"
- If `monitor.sh` has already restarted the encoder, you will see a RESOLVED alert within about 30 s.
- If you get an `encoder speed < 0.97x` alert, the CPU is overloaded. During the stream, lower OBS output to 720p30. Next session, use a bigger plan, or set `-preset superfast` in `ladder.sh`.
- If the origin is fine (`probe.py https://origin.example.com/hls/...`) but the CDN URL is stale, it's the CDN. In an emergency, give viewers the origin URL (only a few dozen viewers).

### The OBS recording is missing or corrupt
**Before** `stop`:
```powershell
python control\livestack.py fetch-recordings --dest E:\livestack-rescue
python control\replay.py run E:\livestack-rescue\recordings\live\test\<file>.mp4 --session-id 20261111-1900
```

### A server was left running
Run `python control\livestack.py orphans`. Check the list, then add `--destroy`.

### Reserved IP "attached to another instance"
An old session server still exists. Run `orphans`, destroy the old one, and re-run `start`. Use `--steal-ip` only if you know the other one is dead.
