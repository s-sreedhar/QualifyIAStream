# livestack: low-cost live + replay streaming

> **LEGACY (2026-11-15).** Production live classes now run on an on-demand Linode VM started from the admin
> (`backend/src/services/linode-live.ts`). See `../../ARCHITECTURE.md` section 7. This Vultr/BlazingCDN system is kept for reference only.

OBS → **MediaMTX + FFmpeg ABR ladder** on a per-session Vultr VPS → **BlazingCDN** → viewers (HLS).
The replay is uploaded once per session as an MP4 to **Cloudflare R2**.
Target: ~100 viewers, 2 h/day, 5 days/week, about **$30–35/month**.

The server exists only while you stream. `start_session` builds it from a golden snapshot.
`stop_session` destroys it, but only after the replay is verified in R2.
The reserved IP and the snapshot are the only things that persist.

```
 OBS ──RTMP:1935──► MediaMTX ──RTSP(local)──► FFmpeg ladder ──► /var/www/hls ──► nginx :443 ──► BlazingCDN ──► viewers
 (ingest.example.com)  │  (publish auth +                 720p/480p/360p          (origin.example.com)   (live.example.com)
                       │   firewall)                       master.m3u8
                       └─► backup recording (fMP4, server disk)
 OBS local recording ──► replay.py (encode MP4 → R2, verify) ──► media.example.com / presigned URLs ──► LMS
```

## Quick start

| I want to… | Do this |
|---|---|
| Stream from OBS on my PC and watch it on my phone (public test link) | `powershell -ExecutionPolicy Bypass -File local\run_local.ps1 -Tunnel` → [docs/LOCAL_TESTING.md](docs/LOCAL_TESTING.md) |
| Run the automated checks on my PC (no cloud) | `powershell -ExecutionPolicy Bypass -File local\test_local.ps1` and `python -m pytest -q tests` |
| Understand and set up Cloudflare R2 (replays) | [docs/R2.md](docs/R2.md), then `python control\replay.py selftest` |
| Set up the cloud side (one time) | [docs/SETUP.md](docs/SETUP.md) |
| Run a session (every stream) | [docs/RUNBOOK.md](docs/RUNBOOK.md) |
| Understand the design and failure handling | [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) |
| Set up and read alerts | [docs/MONITORING.md](docs/MONITORING.md) |
| See what still needs checking before launch | [docs/VERIFY.md](docs/VERIFY.md) |

Every session, in short:

```powershell
.\start_session.ps1 --session 20261111-1900        # ~3-6 min; prints READY + OBS settings
python control\probe.py https://live.example.com/hls/live/test/master.m3u8   # watch while live
# ... stream ...
python control\replay.py run D:\OBS\2026-11-11.mkv --session-id 20261111-1900
.\stop_session.ps1                                 # refuses unless the replay is verified in R2
```

## Repository layout

```
livestack/
├── start_session.{sh,ps1}  stop_session.{sh,ps1}   thin wrappers around control/livestack.py
├── .env.example            every setting, documented (copy to .env)
├── config/
│   ├── mediamtx.yml.tmpl   MediaMTX: RTMP ingest, publish auth, ladder hook, backup recording
│   ├── nginx-origin.conf   HTTPS origin: MIME types, cache headers, CORS, GET/HEAD only
│   ├── r2-cors.json        CORS rules for the replay bucket
│   └── systemd/            mediamtx, livestack-monitor, livestack-boot units
├── server/                 runs on the VPS (Ubuntu 24.04)
│   ├── build_image.sh      one-time golden-image provisioning (+ --finalize)
│   ├── boot.sh             per-session bring-up (idempotent)
│   ├── ladder.sh           FFmpeg 720p/480p/360p HLS ladder (called by MediaMTX)
│   ├── healthcheck.sh      pre-flight: services, RTMP, 3-variant ladder, HTTPS, TLS, speed, NTP, disk
│   ├── monitor.sh          watchdog: stale ladder → auto-restart, speed, disk, NTP, services
│   ├── cert_sync.sh        TLS cert that survives rebuilds (R2 backup, DNS-01, HTTP-01, fallback)
│   ├── alert.sh            webhook alerts (Discord / Slack / ntfy)
│   └── common.sh
├── control/                runs on your PC (Windows/macOS/Linux, Python 3.9+)
│   ├── livestack.py        start / stop / status / health / orphans / snapshot / fetch-recordings
│   ├── replay.py           encode → upload → verify → presign (needs boto3)
│   ├── probe.py            external HLS freshness probe + alerts (stdlib)
│   └── common.py
├── local/                  run the production pipeline on Windows
│   ├── run_local.ps1       live mode: OBS → MediaMTX → ladder → local origin → public tunnel
│   ├── test_local.ps1      automated end-to-end checks (test pattern, no OBS)
│   ├── hls_server.py       local stand-in for nginx (same /hls/ URLs, MIME, cache, CORS)
│   ├── lib.ps1             shared helpers for the two scripts
│   └── player.html         hls.js test player (rendition, latency, buffer)
├── tests/                  pytest: Vultr client, probe parser, env, keys, R2 via a local emulator
├── requirements-dev.txt    test extras (moto)
└── docs/                   ARCHITECTURE, SETUP, R2, RUNBOOK, MONITORING, LOCAL_TESTING, VERIFY
```

`.gitignore` keeps secrets (`.env`, keys, tunnel credentials), runtime state, media and binaries out of git.
Only `.env.example` is committed.

## Tested so far (2026-11-11, on this PC)

- `local\test_local.ps1`: 9/9 checks pass on MediaMTX v1.21.2 and FFmpeg 9.0.2.
  - The config is accepted.
  - The master playlist lists 3 variants (1280x720, 854x480, 640x360).
  - `probe.py` confirms the playlists advance.
  - The origin sends the correct MIME type, cache and CORS headers.
  - Encoder speed is 1.17x.
  - The ladder restarts by itself within 10 s after being killed.
  - Publishing without credentials is rejected, and publishing with credentials is accepted.
  - The backup recording is written.
- `local\run_local.ps1 -Tunnel -TestPattern`: the public `trycloudflare.com` link worked.
  - `probe.py`, run from outside, saw 3 advancing variants.
  - Path traversal and POST were refused.
  - Every process was cleaned up on exit.
- `pytest tests`: 27 passed. This includes `replay.py` against an S3/R2 emulator: multipart, sha256, verify, presign and selftest.
- `bash -n` passes for every shell script.
- `replay.py encode` works, including the re-run that reuses the finished file.
- **Not yet run against real Vultr, R2, BlazingCDN, or with real OBS.** See [docs/VERIFY.md](docs/VERIFY.md).
