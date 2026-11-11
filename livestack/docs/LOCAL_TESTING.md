# Local testing on your PC

Run the **whole production pipeline on this PC**: OBS, MediaMTX, the FFmpeg ladder and an origin web server.
Then share it through a temporary public link (a Cloudflare quick tunnel) so a phone on 4G or someone in another city can watch.
No Vultr, no CDN and no Cloudflare account are needed.

It uses the real `config/mediamtx.yml.tmpl` and `server/ladder.sh`, so anything that works here works on the server.

---

## 1. What each part does

```
 ┌──────── this PC ─────────────────────────────────────────────────────────────────────────────┐
 │                                                                                              │
 │  OBS ──RTMP :1935──► MediaMTX ──RTSP :28554──► FFmpeg ladder ──► local\.live\hls\            │
 │  (camera/screen)     (ingest)    (localhost)   720p/480p/360p      live\test\master.m3u8     │
 │                         │                                          live\test\720p\seg_N.ts   │
 │                         └──► backup recording (local\.live\recordings)          │            │
 │                                                                                 ▼            │
 │                                                     hls_server.py :8090  (stands in for nginx)│
 └─────────────────────────────────────────────────────────────────────────────────┬────────────┘
                                                                                   │ outbound only
                                                            cloudflared quick tunnel (optional)
                                                                                   │
                                                 https://<random-words>.trycloudflare.com  ◄── any phone / browser
```

| Part | Role | Production equivalent |
|---|---|---|
| **OBS** | Captures the camera or screen, encodes it **once** (H.264 + AAC) and **pushes** it over RTMP. | The same: OBS on the teacher's PC. |
| **MediaMTX** | Media server. Accepts the RTMP push (`live/test`), checks publish permission, re-serves the stream internally over RTSP, starts the ladder for every new stream and restarts it if it dies. Also writes a backup recording. | MediaMTX on the Vultr VPS (`ingest.example.com:1935`). |
| **FFmpeg ladder** (`server/ladder.sh`) | Turns the single stream into **3 qualities** (720p / 480p / 360p), cut into 4-second **HLS** segments (`.ts`), plus playlists (`.m3u8`). The player switches quality to match the viewer's internet speed (ABR). | The same script on the VPS. |
| **HLS** | The delivery format: plain files over HTTPS. `master.m3u8` lists the 3 qualities; each `index.m3u8` lists the latest 6 segments and is rewritten every 4 s. Plain files are what lets a CDN cache them. | The same. |
| **`local/hls_server.py`** | Small web server for the HLS folder, with the same URLs (`/hls/...`), MIME types, cache headers and CORS as production nginx. Also serves the test player. | nginx on the VPS (`origin.example.com`). |
| **cloudflared quick tunnel** | Opens an **outbound** connection to Cloudflare and gives you a random public `https://….trycloudflare.com` URL that forwards to `localhost:8090`. No router or firewall changes. | **BlazingCDN** (`live.example.com`). It is only a stand-in: one link, no caching, for testing only. |
| **`local/player.html`** | hls.js test player. Shows the current quality, available qualities, latency, buffer and bandwidth. | Your LMS's video player. |

How long until video appears: OBS buffers about 1 s, the first segment takes 4 s, and the player waits for about 3 segments. So the first picture shows about **8–12 s** after you click *Start Streaming*. After that, viewers watch about **10–15 s behind real time**, which is normal for HLS.

---

## 2. One-time preparation

Already done on this PC:

| Tool | Where | Check |
|---|---|---|
| MediaMTX v1.21.2 | `C:\Users\S Sreedhar Goud\mediamtx.exe` | |
| FFmpeg 9.0.2 | `C:\Users\S Sreedhar Goud\ffmpeg-9.0.2-essentials_build\bin\` | |
| Git Bash | `C:\Program Files\Git\bin\bash.exe` (runs `ladder.sh`) | |
| Python 3.9+ | on PATH | `python --version` |
| cloudflared 2026.10.7 | `E:\Freelancing\QualifyIAStream\tools\cloudflared.exe` | `E:\Freelancing\QualifyIAStream\tools\cloudflared.exe --version` |
| OBS | installed | |

### Install cloudflared (only on a new PC)

`run_local.ps1` finds it in `E:\Freelancing\QualifyIAStream\tools\` or on PATH. To get it, use **one** of these:

```powershell
# a) winget (installs to C:, puts it on PATH)
winget install --id Cloudflare.cloudflared

# b) a single exe on E: (no installer) - what this PC uses
New-Item -ItemType Directory -Force E:\Freelancing\QualifyIAStream\tools | Out-Null
Invoke-WebRequest https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe `
  -OutFile E:\Freelancing\QualifyIAStream\tools\cloudflared.exe
Get-FileHash E:\Freelancing\QualifyIAStream\tools\cloudflared.exe   # compare with the SHA256 listed on the GitHub release page
```

Quick tunnels need **no Cloudflare account and no login**.

### Python packages (for the tests and R2)

```powershell
cd E:\Freelancing\QualifyIAStream\livestack
python -m pip install -r requirements-dev.txt     # boto3, pytest, moto (local R2 emulator)
```

---

## 3. Live mode: OBS → your PC → public link

### Step 1: start the local stack

```powershell
cd E:\Freelancing\QualifyIAStream\livestack
powershell -ExecutionPolicy Bypass -File local\run_local.ps1 -Tunnel
```

It starts MediaMTX, the origin server and the tunnel, then prints something like this:

```
[ok] MediaMTX listening for RTMP on :1935
[ok] local origin on http://127.0.0.1:8090
[ok] tunnel https://licenses-rarely-lcd-months.trycloudflare.com

================ READY ================
OBS (this PC): Settings -> Stream -> Service: Custom
                Server     : rtmp://localhost:1935/live
                Stream key : test
Watch here   : http://127.0.0.1:8090/            (stream list + player)
PUBLIC (any device, any network):
               https://licenses-rarely-lcd-months.trycloudflare.com/player.html?src=/hls/live/test/master.m3u8
               https://licenses-rarely-lcd-months.trycloudflare.com/hls/live/test/master.m3u8   (VLC / probe.py)
22:47:57  waiting for OBS to publish to rtmp://localhost:1935/live/test ...
```

Leave this window open. **Press Q to stop.** Everything it started is stopped and cleaned up, and the public link stops working.

> **"port 1935 is already used by mediamtx"**: the plain MediaMTX from your first test is still running.
> Close its window, or add `-StopExisting`.

### Step 2: OBS

1. **Settings → Stream:** Service **Custom**, Server `rtmp://localhost:1935/live`, Stream Key `test`.
   No password is needed from the same PC; MediaMTX trusts localhost, exactly as on the server.
2. **Settings → Output** (set Output Mode to *Advanced*), *Streaming* tab. These are the same settings you'll use in production:
   - Video encoder: **x264** (or NVENC if you have an NVIDIA GPU)
   - Rate control **CBR**, bitrate **4500 kbps**
   - Keyframe interval **2 s**
   - Preset `veryfast` (x264)
3. **Settings → Video:** Output resolution **1280x720** (or 1920x1080), **30 FPS**.
4. Click **Start Streaming**. Within a few seconds the script window shows:
   ```
   22:48:05  LIVE  live/test  ladder: 720p/480p/360p  speed: 1.69x
   ```

### Step 3: watch

| Where | Open |
|---|---|
| This PC, browser | `http://127.0.0.1:8090/` → click **watch** |
| This PC, VLC | *Media → Open Network Stream* → `http://127.0.0.1:8090/hls/live/test/master.m3u8` |
| **Phone on 4G / any network** | the `PUBLIC` `…trycloudflare.com/player.html?src=…` link (send it over WhatsApp) |
| Phone on the same Wi-Fi, no tunnel | start with `-Lan` and open `http://<PC-IP>:8090/player.html?src=/hls/live/test/master.m3u8`. Allow Python through Windows Firewall if asked. |

The player shows the **Rendition** (720p, 480p or 360p) and switches it automatically.
To see quality switching, open the browser DevTools → *Network* → throttle to "Fast 4G" or "Slow 4G".

### Step 4: check it from outside, like a viewer would

In a second PowerShell window:

```powershell
python control\probe.py https://<your-words>.trycloudflare.com/hls/live/test/master.m3u8 --once
```
`OK ... 3 variants, all advancing and downloadable` means the full chain works over the internet.
Without `--once`, it keeps watching and alerts when the stream goes stale. This is the same monitor you'll point at `live.example.com` in production.

### Reading the status line

| You see | Meaning |
|---|---|
| `waiting for OBS to publish…` | Nothing is arriving. Check the OBS server/key and that OBS says *Live*. |
| `ladder: starting` | MediaMTX has the stream; FFmpeg is producing the first segments (about 4–8 s). |
| `ladder: 720p/480p/360p speed: 1.0x` | Healthy. Speed starts high and **settles near 1.00x**: a live source can't be encoded faster than real time. |
| `speed: 0.9x` in red | The PC can't encode 3 qualities and run OBS at the same time. Lower the OBS output to 720p, or close other programs. (The VPS has 4 dedicated cores; this only affects local tests.) |

### Other options

```powershell
local\run_local.ps1                       # no tunnel, local only
local\run_local.ps1 -Tunnel -TestPattern  # no OBS needed: FFmpeg sends a test card + beep
local\run_local.ps1 -Stream live/class1   # OBS key "class1" instead of "test"
local\run_local.ps1 -HttpPort 8091        # if 8090 is taken (8080 is used by Apache on this PC)
local\run_local.ps1 -NoRecord             # skip the backup recording
local\run_local.ps1 -Seconds 300          # stop automatically after 5 minutes
```

Logs are in `local\.live\logs\` (`mediamtx.log`, `origin.err.log`, `tunnel.err.log`).
Backup recordings are kept in `local\.live\recordings\`. Delete them yourself when you no longer need them.

### About the quick tunnel (read this)

- The URL is **random and changes every run**. Anyone with the link can watch while the script runs, so share it only with testers.
- It is for **testing only**: no uptime guarantee, limited capacity, no caching. Don't use it for real classes; production uses the CDN.
- Only the HTTP side (watching) goes through the tunnel. **OBS must run on this PC or your Wi-Fi** in local mode, because the tunnel doesn't carry RTMP. The production VPS has a public IP for that.
- It is outbound-only. No port is opened on your router or Windows Firewall.
- `hls_server.py` serves only the HLS folder and the player. It allows only GET/HEAD and blocks path tricks (`/hls/../.env` → 404). Nothing else on your PC is reachable.

---

## 4. Automated end-to-end test (no OBS)

```powershell
powershell -ExecutionPolicy Bypass -File local\test_local.ps1
```

It uses separate ports (**19350**, 18554, 18888, 19997, 18080), so it can run alongside live mode.
It takes about 2 minutes and ends with `All checks passed.`

| Check | What it proves |
|---|---|
| `mediamtx_start` | The config template is valid for this MediaMTX version. |
| `ladder_3_variants` | `ladder.sh` produces `master.m3u8` plus 720p/480p/360p with segments. |
| `probe_once` | `probe.py` sees 3 variants that advance and whose segments download. |
| `origin_headers` | The origin sends `.m3u8` as `application/vnd.apple.mpegurl`, `no-cache`, with CORS `*`, as nginx does. |
| `encoder_speed` | The ladder keeps up with real time on this CPU. |
| `ladder_auto_restart` | Kills the encoder; MediaMTX restarts it and the playlist advances again. |
| `auth_rejects_no_credentials` / `auth_accepts_credentials` | Publishing from another machine needs the user/password. |
| `backup_recording` | The server-side safety recording is written. |

Last run on this PC (2026-11-11): **9/9 PASS**. Speed was 1.17x, and the ladder auto-restarted in 10 s.

To keep it running and look at it yourself: `local\test_local.ps1 -KeepRunning -Seconds 600`, then open `http://127.0.0.1:18080/`.

---

## 5. Test the replay path (recording → MP4 → R2)

After a live-mode run you have a backup recording (`local\.live\recordings\live\test\*.mp4`), or use any OBS recording.

```powershell
# 1. encode to the replay format (H.264 CRF 23, AAC, faststart) - no cloud needed
python control\replay.py encode "local\.live\recordings\live\test\<file>.mp4" E:\temp\replay.mp4

# 2a. without an R2 account: the emulator tests (upload, multipart, verify, presign, selftest)
python -m pytest -q tests\test_r2_emulated.py

# 2b. with your real R2 credentials in .env (see docs/R2.md)
python control\replay.py selftest
python control\replay.py upload E:\temp\replay.mp4 --key test/replay.mp4
python control\replay.py verify --key test/replay.mp4 --file E:\temp\replay.mp4
python control\replay.py presign --key test/replay.mp4      # open the link in a browser: it plays
```

How R2 works and how to set it up: **[R2.md](R2.md)**.

---

## 6. Unit tests

```powershell
python -m pytest -q tests        # 27 tests
```

| File | Covers |
|---|---|
| `tests/test_control.py` | Vultr client (retries, no blind create retry, 404s, pagination, never destroys non-livestack servers), env parsing, secrets never sent to the server, CRLF-safe env push, replay keys |
| `tests/test_probe.py` | Playlist parser, stale detection, alert de-duplication |
| `tests/test_r2_emulated.py` | `replay.py` against a local S3-compatible server: single and multipart upload, sha256 metadata, skip-if-identical, re-upload when changed, verify, presigned GET, selftest, CORS file |

---

## 7. The simple setup (no ladder)

This is your plain MediaMTX on the default ports (`C:\Users\S Sreedhar Goud\mediamtx.exe` with its own `mediamtx.yml`), from the very first test. It serves the **single** original quality with MediaMTX's built-in HLS:
- OBS: `rtmp://localhost:1935/live`, key `test`
- Watch: `http://localhost:8888/live/test`

Use it only to check that OBS works at all. Stop it before running `run_local.ps1`, because both want port 1935.

---

## 8. Troubleshooting

| Problem | Fix |
|---|---|
| `port 1935 is already used by mediamtx` | Close the other MediaMTX, or add `-StopExisting`. |
| `port 8090 is already used by …` | Add `-HttpPort 8091`. |
| OBS: *Failed to connect to server* | The script isn't running, or the server isn't `rtmp://localhost:1935/live`. |
| Status stays at `ladder: starting` | Look in `local\.live\logs\mediamtx.log` for `ladder:` lines. Usually the Git Bash or FFmpeg path is wrong: pass `-GitBash` / `-FfmpegDir`. |
| `tunnel did not come up` | Check `local\.live\logs\tunnel.err.log`. Corporate networks sometimes block it; try a phone hotspot. |
| Public link: *DNS_PROBE_FINISHED_NXDOMAIN* | The new hostname takes up to a minute to resolve. Wait and reload. |
| Player: `FATAL networkError: manifestLoadError` | The stream isn't live yet (wait 10 s), or the stream path differs from `-Stream`. |
| Video stutters, speed < 0.97x | The CPU is overloaded locally. Lower the OBS resolution, or use NVENC in OBS. |
| `running scripts is disabled on this system` | Run through `powershell -ExecutionPolicy Bypass -File …` as shown. |
