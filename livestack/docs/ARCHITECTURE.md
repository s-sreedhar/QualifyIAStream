# Architecture

## 1. Components and what each one does

| Part | Where | Job |
|---|---|---|
| **OBS / camera** | streamer | Encodes video once and **pushes** it over RTMP to `rtmp://ingest.example.com/live`, key `<name>?user=…&pass=…`. Also records locally; that recording becomes the replay. |
| **MediaMTX** | VPS | Media router. Accepts RTMP (with credentials), then republishes the stream locally over RTSP. Starts the ladder when a stream goes live (`runOnAvailable`) and restarts it if it exits. Server-side backup recording is disabled for the OBS-local-recording + LMS-upload workflow. |
| **FFmpeg ladder** (`server/ladder.sh`) | VPS | Reads the stream once and makes three renditions: 720p at 2.8 Mbps, 480p at 1.4 Mbps and 360p at 0.8 Mbps, with AAC 96k audio. Writes 4-second HLS segments plus `master.m3u8`. This is the CPU-heavy part: three x264 encodes. |
| **HLS** | – | Each rendition is a playlist (`index.m3u8`) of small `.ts` files. `master.m3u8` lists the renditions so players can switch by bandwidth (ABR). Plain HTTP files, which is why a CDN can cache them. |
| **nginx** | VPS | Read-only HTTPS origin for `/var/www/hls`. Sets correct MIME types. Playlists are `no-cache`; segments are `max-age=60`. |
| **BlazingCDN** | CDN | Pulls from `origin.example.com` and serves ~100 viewers. The VPS serves each file about once instead of 100 times. |
| **Cloudflare R2** | storage | LMS recordings are uploaded separately through the admin dashboard. `livestack-state` holds the TLS certificate backup. |
| **Control tools** | your PC | Current server lifecycle adapter uses the Vultr API; another VPS provider needs a provider adapter. `probe.py` monitors viewer playback. |

## 2. Hostnames

| Hostname | Points to | Cloudflare proxy |
|---|---|---|
| `ingest.example.com` | reserved IP (A) | **DNS only** (grey cloud). Never proxy live video. |
| `origin.example.com` | reserved IP (A) | DNS only |
| `live.example.com` | BlazingCDN hostname (CNAME) | as BlazingCDN specifies |
| `media.example.com` | R2 custom domain | proxied (R2 requires it) |

Set TTL to 60 s. Because the reserved IP survives rebuilds, none of these records change between sessions.

## 3. Session lifecycle

```
start_session                                                       stop_session
  1 create instance from snapshot (label livestack-<session>)         1 verify replay in R2 (size, sha256)
  2 wait active+running                                               2 confirm (type the label)
  3 attach reserved IP (refuses to steal one in use)                  3 DELETE instance, wait until gone
  4 wait SSH (via main IP)                                            4 confirm reserved IP + snapshot still exist
  5 push /etc/livestack/env (only server-safe keys)                   5 archive state/session.json
  6 boot.sh: env check → reserved IP → git sync → NTP → configs
            → firewall → TLS → services → healthcheck.sh
  7 GET https://origin/healthz from your PC
  8 print READY + OBS settings, append boot time to state/boot_times.csv
```

Each step is saved to `state/session.json`. Running `start` again resumes from where it stopped. If the session is already ready, a re-run re-pushes the env and re-runs boot, which is how you apply a changed `.env`, such as a new streamer IP.

## 4. Failure handling (fault tolerance)

| Failure | Detected by | Automatic response | What you do |
|---|---|---|---|
| OBS network blip / reconnect | MediaMTX | Ladder stops, then restarts on reconnect. Segment numbers come from the clock, so players keep moving forward. | Enable OBS auto-reconnect (Settings → Advanced). |
| FFmpeg ladder crashes | MediaMTX | `runOnAvailableRestart` restarts it (11 s in the local test). | – |
| FFmpeg hangs (alive, no output) | `monitor.sh`: 720p playlist older than 20 s | Kills it; MediaMTX restarts it; alert, then RESOLVED. | – |
| Encoder too slow (`speed` < 0.97x for 30 s) | `monitor.sh`, health check | Alert. | Bigger plan, `-preset superfast`, or drop 720p. |
| MediaMTX crashes | systemd | `Restart=always` (2 s); OBS reconnects. | – |
| nginx crashes | systemd drop-in + monitor | Restart, plus an alert. | – |
| Disk fills | `delete_segments` keeps HLS small; monitor alerts at 85%. | Alert. | – |
| TLS cert missing or expiring | `cert_sync.sh` | Snapshot copy → R2 backup → DNS-01 → HTTP-01 → self-signed. Every step except the last keeps the CDN working. | Fix the token or DNS if you get the self-signed alert. |
| Let's Encrypt rate limit (5 duplicate certs/week) | – | Avoided: the cert is reused from R2 and renewed only when under 21 days remain. | – |
| Config git repo unreachable | `boot.sh` | Uses the copy baked into the snapshot; alert. | – |
| Server reboots mid-session | systemd | `livestack-boot.service` re-applies the config from the saved env; services auto-start. | – |
| Vultr API 429/5xx/timeouts | client | Exponential backoff (6 tries). Create is never retried blindly; it is looked up by label instead. | – |
| `start` interrupted (Ctrl+C, laptop sleep) | state file | Re-run resumes. | Re-run `start`. |
| Reserved IP still on an old server | `start` | Refuses, and names the holder. | `orphans`, then `--steal-ip` if appropriate. |
| Forgotten server (billing!) | `orphans` (schedule it) | Alert; `--destroy` optional. | – |
| Replay upload fails | `replay.py` | Multipart upload with adaptive retries and 3 full attempts; a re-run skips what is already uploaded. | Re-run. |
| `stop` before the replay is safe | `stop` | **Refuses** to destroy (exit 3). | Upload, or `fetch-recordings`. |
| Camera/OBS recording lost | – | Server keeps a backup recording of the incoming stream. | `fetch-recordings` **before** `stop`. |
| CDN outage | `probe.py` against the CDN URL | Alert. | Temporarily give viewers the origin URL. Emergency only: 100 × 2.9 Mbps ≈ 290 Mbps from the VPS. |
| Streamer's IP changed (dynamic ISP) | OBS cannot connect | – | Update `STREAMER_IPS`, re-run `start` (~1 min). Or use `any`; publish auth still protects ingest. |

## 5. Security model

- **Ingest:** the firewall allows only `STREAMER_IPS`, and MediaMTX also requires `PUBLISH_USER`/`PUBLISH_PASS`. Two independent layers.
- **Local-only ports:** RTSP 8554, MediaMTX HLS 8888, API 9997 and metrics 9998 are bound to 127.0.0.1 and also blocked by ufw. MoQ is disabled.
- **Origin:** nginx serves only `/hls/` and `/healthz`, with GET/HEAD only and no directory listing. `.tmp` files are never served.
- **Path names** coming from the publisher are validated before they touch the filesystem (`ladder.sh`).
- **SSH:** keys only, no passwords (`build_image.sh`). It can be limited to `ADMIN_IPS`.
- **Secrets:** `.env` is git-ignored. The server only receives the keys in `SERVER_ENV_KEYS` (see `control/livestack.py`). The Vultr API key and the replay-bucket write key never leave your PC. The R2 state token should be scoped to the state bucket only. The Cloudflare token should be `Zone:DNS:Edit` on one zone.
- Unattended upgrades are disabled in the image so a package restart can't interrupt a stream. Rebuild the snapshot monthly with updates (see SETUP §7).

## 6. Deviations from the original brief (and why)

| Brief | Implemented | Why |
|---|---|---|
| `runOnReady`, `runOnReadyRestart` | `runOnAvailable`, `runOnAvailableRestart` | Renamed in MediaMTX v1.21. The old names log deprecation warnings. |
| Shell scripts using Vultr CLI/curl | Python stdlib CLI plus thin `.sh`/`.ps1` wrappers | Works the same on Windows. Adds retries, resume, and safe JSON handling. No `jq`/`vultr-cli` needed on the PC. |
| Let's Encrypt via certbot | Plus R2 backup/restore and fallbacks | Rebuilding 5×/week would hit LE's duplicate-certificate limit. |
| Firewall only | Firewall plus publish credentials | Home IPs change; credentials keep ingest safe if you must use `any`. |
| HLS flags `independent_segments` | Also `delete_segments`, `temp_file`, `program_date_time`, epoch numbering, `fps=30`, forced keyframes every 4 s | Bounded disk use, no half-written segments, smooth restarts, exact 4 s segments whatever the source fps. |
| Server-side backup recording | Disabled by default | OBS local recording is uploaded through the LMS separately. |

## 7. Costs (existing Vultr + CDN estimate; re-price for the selected provider)

| Item | Monthly |
|---|---|
| BlazingCDN (~3 TB) | $25 (minimum) |
| Vultr 4 vCPU, ~55 h | ~$3–6 (VERIFY) |
| Snapshot | ~$0.50 |
| Reserved IP | VERIFY |
| R2 (MP4 replays, +~60 GB/month) | ~$3, rising ~$1/month |
| **Total** | **~$30–35** |

Delivery scaling: 10 TB ≈ $48, 30 TB ≈ $135, 100 TB ≈ $415.
Bandwidth per viewer is at most about 2.9 Mbps (720p plus audio). For 100 viewers × 2 h that is ≈ 260 GB per session at worst, or ~5 TB/month if everyone watches 720p. The ~3 TB estimate assumes a mix of renditions.
