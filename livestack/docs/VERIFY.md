# Open items to verify before launch

Record each result here, with the date, as you check it.

| # | Item | How to verify | Result |
|---|---|---|---|
| 1 | Vultr 4 vCPU price in Mumbai | Vultr pricing calculator, or `GET /v2/plans?type=vc2`; check `locations` includes `bom`. Update `VULTR_PLAN`. | |
| 2 | Reserved IP monthly charge | Vultr pricing page, or the invoice after the first month. | |
| 3 | Boot time (snapshot → READY) | `start` prints it and appends it to `state/boot_times.csv`. Take the median of 3 runs. Start sessions this much earlier. | |
| 4 | Snapshot of a configured, running instance works | SETUP §6–7: a server created from the snapshot passes `start` with all PASS. | |
| 5 | Reserved IP is reachable after attach on Ubuntu 24.04 | After `start`, `curl https://origin.example.com/healthz` works from outside. If Vultr already configures it, set `CONFIGURE_RESERVED_IP=no`. | |
| 6 | Vultr `user_data` / `sshkey_id` with snapshot deploys | Not relied on: SSH keys are baked into the image and the env is pushed over SSH. Note it only. | n/a |
| 7 | BlazingCDN live HLS support (short-TTL playlists, origin headers honoured) | Run `probe.py` against `live.` for 30 min: no stale alerts, and latency within 2–3 s of the origin. | |
| 8 | BlazingCDN SLA and throttling under sustained load | Ask support in writing. During the 2-week pilot, compare with Bunny Standard. | |
| 9 | BlazingCDN trial terms (14 days, minimum waived?) | Account page or support. | |
| 10 | Cloudflare terms for serving MP4 replays from R2 | Read the current Service-Specific Terms (CDN / R2 sections). Serving directly from R2 (custom domain or presigned URL) is the intended pattern; don't proxy non-Cloudflare-hosted video. | |
| 11 | Local ladder `speed >= 1.0x` and playback | 2026-11-11: `local/test_local.ps1` 9/9, speed 1.17x. `run_local.ps1 -Tunnel -TestPattern`: `probe.py` through the public trycloudflare URL reported 3 variants advancing. Still to do: stream from **real OBS** and watch on a phone over 4G. | partial |
| 12 | Server ladder speed on the chosen plan | `healthcheck.sh` `encoder_speed`, plus `monitor.sh` during a real 2-hour stream: no speed alerts. | |
| 13 | Let's Encrypt via DNS-01 + R2 backup | First boot logs `certificate backed up to R2`. Second session logs `restored /etc/letsencrypt from R2`. | |
| 14 | Replay upload end to end | `replay.py run` on a real 2-hour recording: manifest written, `stop` accepts it, and a `presign` URL plays in a browser. | |
| 15 | Alert delivery | Every source (monitor, probe, orphans) delivers to your phone. | |
| 16 | R2 tokens and bucket scoping | `replay.py selftest` and `selftest --state`: all PASS, with no `[WARN] token scope`. (The code was tested against the moto emulator on 2026-11-11; this checks the real account.) | |
| 17 | R2 prices (storage, Class A/B, free tier) | Cloudflare R2 pricing page. Update docs/R2.md §2 if they changed. | |
| 18 | R2 CORS from the real LMS page | Paste `config/r2-cors.json` in the dashboard with your LMS origin. A presigned replay plays inside the LMS page without CORS errors in the browser console. | |
