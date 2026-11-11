# One-time setup

Do these once, in order. Everything marked **VERIFY** has a matching line in [VERIFY.md](VERIFY.md).
Keep the project on **E:** (`E:\Freelancing\QualifyIAStream\livestack`).

## 0. Your PC

```powershell
cd E:\Freelancing\QualifyIAStream\livestack
python -m pip install -r requirements-dev.txt         # boto3 + pytest + moto (R2 emulator)
copy .env.example .env                                # then edit .env
python -m pytest -q tests                              # 27 tests should pass
powershell -ExecutionPolicy Bypass -File local\test_local.ps1   # 9 checks should pass
powershell -ExecutionPolicy Bypass -File local\run_local.ps1 -Tunnel   # OBS -> public test link (docs/LOCAL_TESTING.md)
ssh-keygen -t ed25519 -f $HOME\.ssh\livestack_ed25519 -N '""'    # SSH key for the servers
```

Generate the publish password with:
`python -c "import secrets;print(secrets.token_urlsafe(24))"` → `PUBLISH_PASS`.

## 1. Domain and DNS (Cloudflare)

Create the records from [ARCHITECTURE.md §2](ARCHITECTURE.md#2-hostnames). `ingest` and `origin` must be **DNS only** (grey cloud), with TTL 60.
You create the `ingest` and `origin` A records after step 2, once you know the reserved IP.

Create a Cloudflare API token: *My Profile → API Tokens → Create → Edit zone DNS*, scoped to this zone only. Put it in `CF_DNS_API_TOKEN`.

## 2. Vultr

1. **API key:** *Account → API*. Enable it and restrict access to your PC's IP. Put it in `VULTR_API_KEY`.
2. **SSH key:** *Account → SSH Keys → Add* the contents of `livestack_ed25519.pub`. Optionally put its id in `VULTR_SSH_KEY_IDS`.
3. **Reserved IP:** *Network → Reserved IPs → Add*, IPv4, region Mumbai (`bom`). Put its id in `VULTR_RESERVED_IP_ID`.
   Point the `ingest` and `origin` A records at it. **VERIFY** its monthly price.
4. **Plan:** confirm the 4 vCPU plan id and price for the region. Put it in `VULTR_PLAN`. **VERIFY**
   `curl -H "Authorization: Bearer %VULTR_API_KEY%" "https://api.vultr.com/v2/plans?type=vc2"`
5. Set budget/billing alerts in the Vultr dashboard.

## 3. Cloudflare R2

Full walkthrough, with what each piece is: **[R2.md](R2.md)**. In short:

1. Create bucket `lms-replays` (location: automatic) and bucket `livestack-state` (private, for the TLS backup).
2. **Replay token** (your PC only): *R2 → Manage API tokens → Create*, **Object Read & Write**, bucket `lms-replays`.
   Put it in `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID` and `R2_SECRET_ACCESS_KEY`.
3. **State token** (copied to the server): Object Read & Write, bucket `livestack-state` only.
   Put it in `STATE_R2_*`.
4. CORS on the replay bucket: edit `config/r2-cors.json` (your LMS origin), then paste it in the dashboard: `lms-replays` → *Settings → CORS Policy*.
   (`python control\replay.py cors` works only with an Admin token; Object tokens may not change bucket settings.)
5. Optional public access: attach the custom domain `media.example.com` to `lms-replays`. Leave it off if the LMS uses presigned URLs (recommended).
6. Set a budget alert (*Billing → Notifications*).
7. **Test both tokens:** `python control\replay.py selftest` and `python control\replay.py selftest --state`. You want `all checks passed` and no `[WARN]`.

## 4. Alerts

Pick one and put its URL in `ALERT_WEBHOOK_URL`:
- **Discord:** channel → Integrations → Webhooks → copy URL.
- **Slack:** an Incoming Webhook URL.
- **ntfy** (no account): `https://ntfy.sh/<long-random-topic>`, then subscribe to it in the ntfy phone app.

Test it: `python -c "import sys;sys.path.insert(0,'control');from common import *;load_env();print(alert('test alert','info'))"` (prints `True` if delivered).

## 5. Build the golden image

1. In Vultr, deploy **Ubuntu 24.04 x64** in `bom` on `VULTR_PLAN`, with your SSH key.
   Label it `golden-build`. Do **not** prefix it with `livestack-`, so `orphans` ignores it.
2. Copy the repo and provision it:
   ```powershell
   scp -i $HOME\.ssh\livestack_ed25519 -r E:\Freelancing\QualifyIAStream\livestack root@<IP>:/root/livestack
   ssh -i $HOME\.ssh\livestack_ed25519 root@<IP> "bash /root/livestack/server/build_image.sh"
   ```
   (If you use a git repo, set `CONFIG_REPO_URL` and `boot.sh` will pull the latest config on every boot.)
3. **Test it before snapshotting.** Temporarily point the reserved IP at it: attach it in the Vultr UI.
   Then push an env file and run boot:
   ```powershell
   python -c "import sys;sys.path.insert(0,'control');import common,livestack;common.load_env();print(livestack.server_env_text())" | ssh -i $HOME\.ssh\livestack_ed25519 root@<IP> "umask 077; cat > /etc/livestack/env"
   ssh -i $HOME\.ssh\livestack_ed25519 root@<IP> /opt/livestack/server/boot.sh
   ```
   You want `RESULT: READY`. This first boot also issues the TLS certificate and backs it up to R2.
   Stream from OBS and watch `https://origin.example.com/hls/live/test/master.m3u8` in `local\player.html`.
4. Clean it: `ssh … root@<IP> "bash /opt/livestack/server/build_image.sh --finalize"`, then detach the reserved IP in the UI.

## 6. Snapshot

```powershell
python control\livestack.py snapshot --instance-id <golden-build instance id>
```
This waits until the snapshot is complete and prints `VULTR_SNAPSHOT_ID=…`. Put that in `.env`.
**VERIFY:** whether Vultr snapshots a running instance cleanly (it is documented to; confirm by doing step 7).
Then destroy `golden-build`.

## 7. First real dry run

```powershell
.\start_session.ps1 --session dryrun-1
```
Note the boot time. It is also appended to `state\boot_times.csv` (**VERIFY**: boot time).
Then stream, run `probe.py`, upload a test replay, and `.\stop_session.ps1`.

**Monthly maintenance:** repeat steps 5–6 to pick up security updates. Keep the previous snapshot until the new one has had a good session, then delete the old one.

## 8. BlazingCDN

1. Create a pull zone: origin `https://origin.example.com`, with host header `origin.example.com`.
2. Caching: **honour origin headers** (`.m3u8` no-cache, `.ts` 60 s). Don't cache 404s for more than about 2 s, or a stream start can stay "missing".
3. Add the CNAME `live.example.com` and its TLS certificate in BlazingCDN.
4. Test while a session is running:
   `python control\probe.py https://live.example.com/hls/live/test/master.m3u8 --once`
5. **VERIFY:** live HLS support, SLA and throttling. Run a two-week pilot with Bunny Standard as the fallback.

## 9. Schedule the orphan check (protects your bill)

Windows Task Scheduler, every 2 hours:
```powershell
schtasks /Create /SC HOURLY /MO 2 /TN "livestack-orphans" /TR "python E:\Freelancing\QualifyIAStream\livestack\control\livestack.py orphans"
```
It alerts when any `livestack-*` server is older than `MAX_SESSION_HOURS` (default 4).
