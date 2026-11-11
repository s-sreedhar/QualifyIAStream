#!/usr/bin/env python3
"""livestack control CLI - per-session Vultr origin lifecycle (stdlib only).

  start             create server from snapshot, attach reserved IP, boot, health check
  stop              verify the replay is in R2, then destroy the server
  status            show session state, instance and reserved-IP binding
  health            re-run the on-server health check
  fetch-recordings  download the server-side backup recordings (before stop!)
  orphans           find (and optionally destroy) forgotten livestack servers
  snapshot          create the golden snapshot from a configured instance

Settings come from the environment or livestack/.env (see .env.example).

Fault tolerance
  * Progress is saved in state/session.json after every step. If `start` is
    interrupted (Ctrl+C, network, laptop sleep), run it again: it resumes.
  * Instance creation is never blindly retried (it is not idempotent); after an
    ambiguous error the instance is looked up by its unique label instead.
  * Other API calls retry with exponential backoff on HTTP 429/5xx/timeouts.
  * `stop` refuses to destroy the server until the replay upload is verified.
  * `orphans` catches servers that were never stopped (they bill every hour).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (ROOT, TransientError, alert, load_env, log, require, retry,  # noqa: E402
                    setup_logging, wait_until)

STATE_DIR = ROOT / "state"
STATE_FILE = STATE_DIR / "session.json"
HISTORY_DIR = STATE_DIR / "history"
BOOT_TIMES = STATE_DIR / "boot_times.csv"
LABEL_PREFIX = "livestack-"
SESSION_RE = re.compile(r"^[A-Za-z0-9-]{1,40}$")

# Only these settings are copied to the server. VULTR_API_KEY and the replay
# bucket's write credentials never leave the control machine.
SERVER_ENV_KEYS = (
    "ORIGIN_HOST", "INGEST_HOST", "RESERVED_IP", "CONFIGURE_RESERVED_IP",
    "PUBLISH_USER", "PUBLISH_PASS", "STREAMER_IPS", "ADMIN_IPS",
    "ACME_EMAIL", "CF_DNS_API_TOKEN",
    "STATE_BUCKET", "STATE_R2_ACCOUNT_ID", "STATE_R2_ACCESS_KEY_ID", "STATE_R2_SECRET_ACCESS_KEY",
    "ALERT_WEBHOOK_URL", "CONFIG_REPO_URL", "CONFIG_REPO_REF", "RECORD_BACKUP",
)


# =========================================================================== Vultr API
class VultrError(RuntimeError):
    def __init__(self, msg: str, status: int | None = None):
        super().__init__(msg)
        self.status = status


class Vultr:
    def __init__(self, api_key: str, base: str | None = None, timeout: float = 30, retry_delay: float = 2.0):
        self.key = api_key
        self.base = (base or os.environ.get("VULTR_API_BASE") or "https://api.vultr.com/v2").rstrip("/")
        self.timeout = timeout
        self.retry_delay = retry_delay

    def _once(self, method: str, path: str, body: dict | None = None) -> dict:
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(
            self.base + path, data=data, method=method,
            headers={"Authorization": f"Bearer {self.key}", "Content-Type": "application/json",
                     "User-Agent": "livestack-control/1.0"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                raw = r.read()
                return json.loads(raw) if raw.strip() else {}
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:300]
            msg = f"{method} {path} -> HTTP {e.code}: {detail}"
            if e.code == 429 or e.code >= 500:
                raise TransientError(msg) from None
            raise VultrError(msg, e.code) from None
        except (urllib.error.URLError, TimeoutError, ConnectionError, socket.timeout) as e:
            raise TransientError(f"{method} {path}: {e}") from None

    def call(self, method: str, path: str, body: dict | None = None, attempts: int = 6) -> dict:
        return retry(lambda: self._once(method, path, body), attempts=attempts, base_delay=self.retry_delay,
                     retry_on=(TransientError,), what=f"{method} {path}")

    # -- instances
    def list_instances(self, label: str | None = None) -> list[dict]:
        out, cursor = [], ""
        while True:
            q = {"per_page": "100"}
            if label:
                q["label"] = label
            if cursor:
                q["cursor"] = cursor
            d = self.call("GET", "/instances?" + urllib.parse.urlencode(q))
            out += d.get("instances", [])
            cursor = ((d.get("meta") or {}).get("links") or {}).get("next") or ""
            if not cursor:
                return out

    def get_instance(self, iid: str) -> dict | None:
        try:
            return self.call("GET", f"/instances/{iid}")["instance"]
        except VultrError as e:
            if e.status == 404:
                return None
            raise

    def create_instance(self, body: dict) -> dict:
        return self._once("POST", "/instances", body)["instance"]  # NOT retried: not idempotent

    def delete_instance(self, iid: str) -> None:
        try:
            self.call("DELETE", f"/instances/{iid}")
        except VultrError as e:
            if e.status != 404:
                raise

    # -- reserved IPs
    def get_reserved_ip(self, rid: str) -> dict:
        return self.call("GET", f"/reserved-ips/{rid}")["reserved_ip"]

    def attach_reserved_ip(self, rid: str, iid: str) -> None:
        self.call("POST", f"/reserved-ips/{rid}/attach", {"instance_id": iid})

    def detach_reserved_ip(self, rid: str) -> None:
        self.call("POST", f"/reserved-ips/{rid}/detach", {})

    # -- snapshots
    def create_snapshot(self, iid: str, description: str) -> dict:
        return self._once("POST", "/snapshots", {"instance_id": iid, "description": description})["snapshot"]

    def get_snapshot(self, sid: str) -> dict | None:
        try:
            return self.call("GET", f"/snapshots/{sid}")["snapshot"]
        except VultrError as e:
            if e.status == 404:
                return None
            raise


# =========================================================================== state
def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}


def save_state(st: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, indent=2), encoding="utf-8")
    os.replace(tmp, STATE_FILE)  # atomic: never a half-written state file


def archive_state(st: dict) -> None:
    if not st:
        return
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    name = f"{st.get('session_id', 'unknown')}-{int(time.time())}.json"
    (HISTORY_DIR / name).write_text(json.dumps(st, indent=2), encoding="utf-8")
    STATE_FILE.unlink(missing_ok=True)


def record_boot_time(st: dict) -> None:
    new = not BOOT_TIMES.exists()
    t = st.get("timings", {})
    with BOOT_TIMES.open("a", encoding="utf-8") as f:
        if new:
            f.write("session_id,region,plan,created_s,active_s,ip_attached_s,ssh_s,boot_done_s,result\n")
        f.write(",".join(str(x) for x in (
            st.get("session_id"), os.environ.get("VULTR_REGION"), os.environ.get("VULTR_PLAN"),
            t.get("created"), t.get("active"), t.get("ip_attached"), t.get("ssh"), t.get("boot_done"),
            st.get("stage"))) + "\n")


# =========================================================================== ssh
def ssh_opts() -> list[str]:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    opts = ["-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
            "-o", f"UserKnownHostsFile={STATE_DIR / 'known_hosts'}",
            "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=4"]
    key = os.environ.get("SSH_KEY_PATH")
    if key:
        opts += ["-i", os.path.expanduser(key)]
    return opts


def ssh_target(host: str) -> str:
    return f"{os.environ.get('SSH_USER', 'root')}@{host}"


def ssh(host: str, remote_cmd: str, input_text: str | None = None, timeout: float = 900,
        check: bool = True, capture: bool = False) -> subprocess.CompletedProcess:
    # Bytes, not text mode: on Windows text mode would turn "\n" into "\r\n" in the
    # env file we push, and bash would then read every value with a trailing \r.
    data = input_text.encode("utf-8") if input_text is not None else None
    r = subprocess.run(["ssh", *ssh_opts(), ssh_target(host), remote_cmd], input=data,
                       timeout=timeout, capture_output=capture)
    if capture:
        r.stdout = (r.stdout or b"").decode("utf-8", "replace")
        r.stderr = (r.stderr or b"").decode("utf-8", "replace")
    if check and r.returncode != 0:
        raise RuntimeError(f"ssh {host} '{remote_cmd}' failed with exit code {r.returncode}")
    return r


def tcp_open(host: str, port: int, timeout: float = 3) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def https_ok(url: str, timeout: float = 10) -> tuple[bool, str]:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "livestack-control/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status == 200, f"HTTP {r.status}"
    except Exception as e:
        return False, str(e)


def server_env_text() -> str:
    lines = ["# Written by `livestack.py start`. Re-run start to change it; do not edit on the server."]
    for k in SERVER_ENV_KEYS:
        v = os.environ.get(k)
        if v is None or v == "":
            continue
        if "\n" in v or "'" in v:
            raise SystemExit(f"{k} must not contain newlines or single quotes")
        lines.append(f"{k}='{v}'")
    return "\n".join(lines) + "\n"


def confirm(prompt: str, expected: str, assume_yes: bool) -> None:
    if assume_yes:
        return
    if not sys.stdin.isatty():
        raise SystemExit("refusing without confirmation in a non-interactive shell (pass --yes)")
    if input(f"{prompt}\nType '{expected}' to continue: ").strip() != expected:
        raise SystemExit("aborted")


def destroy_instance(v: Vultr, inst: dict) -> None:
    label = inst.get("label", "")
    if not label.startswith(LABEL_PREFIX):
        raise SystemExit(f"refusing to destroy instance {inst['id']} - label '{label}' is not a livestack server")
    log.info("destroying %s (%s, %s)", label, inst["id"], inst.get("main_ip"))
    v.delete_instance(inst["id"])
    wait_until(lambda: v.get_instance(inst["id"]) is None, 600, 10, f"instance {inst['id']} to be deleted")
    log.info("destroyed %s", label)


# =========================================================================== commands
def cmd_start(a: argparse.Namespace) -> int:
    api_key, region, plan, snapshot_id, rip_id = require(
        "VULTR_API_KEY", "VULTR_REGION", "VULTR_PLAN", "VULTR_SNAPSHOT_ID", "VULTR_RESERVED_IP_ID")
    require("ORIGIN_HOST", "PUBLISH_USER", "PUBLISH_PASS", "STREAMER_IPS")
    v = Vultr(api_key)
    t0 = time.monotonic()

    def mark(st: dict, stage: str) -> None:
        st["stage"] = stage
        st.setdefault("timings", {})[stage] = round(time.monotonic() - t0)
        save_state(st)
        log.info("[%4ds] %s", st["timings"][stage], stage)

    # ---- 1. create (or resume / adopt)
    st = load_state()
    if st.get("instance_id"):
        if v.get_instance(st["instance_id"]):
            log.info("resuming session %s (instance %s, last stage: %s)", st["session_id"], st["instance_id"], st.get("stage"))
        else:
            log.warning("state refers to instance %s which no longer exists - starting fresh", st["instance_id"])
            archive_state(st)
            st = {}
    if not st.get("instance_id"):
        session_id = a.session or dt.datetime.now().strftime("%Y%m%d-%H%M")
        if not SESSION_RE.match(session_id):
            raise SystemExit("--session must be 1-40 chars of letters, digits and '-'")
        label = LABEL_PREFIX + session_id
        existing = v.list_instances(label=label)
        if existing:
            inst = existing[0]
            log.info("adopting existing instance %s with label %s", inst["id"], label)
        else:
            body = {"region": region, "plan": plan, "snapshot_id": snapshot_id, "label": label,
                    "hostname": label, "backups": "disabled", "enable_ipv6": False, "tags": ["livestack"]}
            if os.environ.get("VULTR_SSH_KEY_IDS"):
                body["sshkey_id"] = [s.strip() for s in os.environ["VULTR_SSH_KEY_IDS"].split(",") if s.strip()]
            if os.environ.get("VULTR_FIREWALL_GROUP_ID"):
                body["firewall_group_id"] = os.environ["VULTR_FIREWALL_GROUP_ID"]
            log.info("creating %s (%s in %s from snapshot %s)", label, plan, region, snapshot_id)
            try:
                inst = v.create_instance(body)
            except TransientError as e:
                log.warning("create returned an ambiguous error (%s); checking whether it was created", e)
                time.sleep(15)
                found = v.list_instances(label=label)
                if not found:
                    raise
                inst = found[0]
        st = {"session_id": session_id, "label": label, "instance_id": inst["id"],
              "created_at": now_iso(), "timings": {}}
        mark(st, "created")
    iid = st["instance_id"]

    try:
        # ---- 2. wait for active + running
        def active() -> dict | None:
            i = v.get_instance(iid)
            if i is None:
                raise RuntimeError(f"instance {iid} disappeared while booting")
            return i if i.get("status") == "active" and i.get("power_status") == "running" else None

        inst = wait_until(active, a.boot_timeout, 10, "instance to become active")
        st["main_ip"] = inst.get("main_ip")
        mark(st, "active")

        # ---- 3. reserved IP
        rip = v.get_reserved_ip(rip_id)
        ip = rip.get("subnet")
        holder = rip.get("instance_id") or ""
        if holder and holder != iid:
            other = v.get_instance(holder)
            if other and not a.steal_ip:
                raise RuntimeError(
                    f"reserved IP {ip} is attached to {other.get('label')} ({holder}). "
                    "Run `livestack.py orphans` to inspect, or pass --steal-ip to move it.")
            log.warning("detaching reserved IP %s from %s", ip, holder)
            v.detach_reserved_ip(rip_id)
            time.sleep(5)
        if holder != iid:
            retry(lambda: v.attach_reserved_ip(rip_id, iid), attempts=8, base_delay=5,
                  retry_on=(VultrError, TransientError), what="attach reserved IP")
            wait_until(lambda: v.get_reserved_ip(rip_id).get("instance_id") == iid, 180, 5, "reserved IP attachment")
        st["reserved_ip"] = ip
        os.environ["RESERVED_IP"] = ip
        mark(st, "ip_attached")

        # ---- 4. SSH (via main IP: always reachable, even if the reserved IP is misconfigured)
        host = st["main_ip"]
        wait_until(lambda: tcp_open(host, 22), a.boot_timeout, 5, f"SSH port on {host}")
        wait_until(lambda: ssh(host, "true", check=False, timeout=30, capture=True).returncode == 0,
                   180, 5, f"SSH login to {host}")
        mark(st, "ssh")

        # ---- 5. push session env + run boot.sh (includes health check)
        ssh(host, "umask 077; mkdir -p /etc/livestack && cat > /etc/livestack/env.new "
                  "&& mv /etc/livestack/env.new /etc/livestack/env", input_text=server_env_text(), timeout=60)
        log.info("running boot.sh on %s (config, firewall, TLS, services, health check)...", host)
        boot = ssh(host, "/opt/livestack/server/boot.sh", check=False, timeout=a.boot_script_timeout)
        mark(st, "boot_done")

        # ---- 6. verify from outside, the way the CDN will reach it
        origin = os.environ["ORIGIN_HOST"]
        ext_ok, ext_msg = False, ""
        for _ in range(6):
            ext_ok, ext_msg = https_ok(f"https://{origin}/healthz")
            if ext_ok:
                break
            time.sleep(5)
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception as e:
        st["stage_error"] = str(e)
        save_state(st)
        alert(f"start_session failed for {st['label']}: {e}. The server is still running (billing); "
              "re-run `start` to resume or `stop --skip-replay-check` to destroy.", "error")
        if a.destroy_on_failure:
            inst = v.get_instance(iid)
            if inst:
                destroy_instance(v, inst)
            archive_state(st)
        raise

    ok = boot.returncode == 0 and ext_ok
    st["stage"] = "ready" if ok else "unhealthy"
    st["ready_at"] = now_iso() if ok else None
    save_state(st)
    record_boot_time(st)

    print()
    print("=" * 70)
    print(f" Session   : {st['session_id']}   instance {iid}")
    print(f" Server    : {st['main_ip']}  (reserved IP {st['reserved_ip']})")
    print(f" Boot time : {st['timings'].get('boot_done')}s total "
          f"(active {st['timings'].get('active')}s, ssh {st['timings'].get('ssh')}s) -> {BOOT_TIMES.name}")
    print(f" On-server health check : {'PASS' if boot.returncode == 0 else 'FAIL (see output above)'}")
    print(f" https://{origin}/healthz from here : {'PASS' if ext_ok else 'FAIL - ' + ext_msg}")
    print("-" * 70)
    if ok:
        ingest = os.environ.get("INGEST_HOST", st["reserved_ip"])
        live = os.environ.get("LIVE_HOST", origin)
        print(" READY - the streamer can go live.")
        print(f"   OBS Server     : rtmp://{ingest}/live")
        print(f"   OBS Stream key : <name>?user={os.environ['PUBLISH_USER']}&pass=<PUBLISH_PASS from .env>")
        print(f"   Viewers (CDN)  : https://{live}/hls/live/<name>/master.m3u8")
        print(f"   Origin (debug) : https://{origin}/hls/live/<name>/master.m3u8")
        print(f"   Watch it       : python control/probe.py https://{live}/hls/live/<name>/master.m3u8")
    else:
        print(" NOT READY - do not go live. Fix the failures, then re-run `start` (it resumes).")
        print("   The server is running and billing. To abandon: stop --skip-replay-check")
        alert(f"session {st['session_id']} NOT READY (health={boot.returncode}, external={ext_msg})", "error")
    print("=" * 70)
    return 0 if ok else 2


def check_replay(a: argparse.Namespace, st: dict) -> tuple[bool, str]:
    from replay import load_manifest, verify_object  # needs boto3; imported only here
    key, size, sha = a.replay_key, None, None
    if a.replay_file:
        size = Path(a.replay_file).stat().st_size
    elif a.replay_size is not None:
        size = a.replay_size
    if not key and st.get("session_id"):
        m = load_manifest(st["session_id"])
        if m:
            key, size, sha = m["key"], m["size"], m.get("sha256")
            log.info("using upload manifest for session %s: %s", st["session_id"], key)
    if not key or size is None:
        return False, ("no replay to verify: pass --replay-key with --replay-file/--replay-size, or upload with "
                       "`python control/replay.py run <file> --session-id <id>` first")
    return verify_object(key, size, sha)


def cmd_stop(a: argparse.Namespace) -> int:
    v = Vultr(require("VULTR_API_KEY")[0])
    st = load_state()
    iid = a.instance_id or st.get("instance_id")
    if not iid:
        raise SystemExit("no active session in state/session.json (pass --instance-id)")
    inst = v.get_instance(iid)
    if inst is None:
        log.info("instance %s is already gone", iid)
        archive_state(st)
        return 0

    if a.skip_replay_check:
        log.warning("skipping replay verification - the server's backup recording will be LOST")
        confirm(f"Destroy {inst.get('label')} WITHOUT a verified replay?", inst.get("label", ""), a.yes)
    else:
        ok, msg = check_replay(a, st)
        if not ok:
            alert(f"stop refused for {inst.get('label')}: replay not verified ({msg})", "error")
            print(f"\nNOT destroying the server: {msg}")
            print("Options: upload the replay (control/replay.py run ...), or rescue the server copy first:")
            print("         python control/livestack.py fetch-recordings --dest <folder>")
            return 3
        log.info("replay verified: %s", msg)
        confirm(f"Destroy {inst.get('label')} ({iid})?", inst.get("label", ""), a.yes)

    destroy_instance(v, inst)

    # The persistent resources must survive.
    rid, sid = os.environ.get("VULTR_RESERVED_IP_ID"), os.environ.get("VULTR_SNAPSHOT_ID")
    if rid:
        try:
            log.info("reserved IP kept: %s", v.get_reserved_ip(rid).get("subnet"))
        except Exception as e:
            alert(f"reserved IP {rid} check failed after stop: {e}", "error")
    if sid:
        snap = v.get_snapshot(sid)
        log.info("snapshot kept: %s", snap.get("description") if snap else "NOT FOUND!")
        if not snap:
            alert(f"golden snapshot {sid} not found!", "error")
    st["stage"] = "destroyed"
    st["destroyed_at"] = now_iso()
    archive_state(st)
    print("Session stopped. Server destroyed; reserved IP and snapshot kept.")
    return 0


def cmd_status(a: argparse.Namespace) -> int:
    st = load_state()
    print(json.dumps(st, indent=2) if st else "no active session")
    if not os.environ.get("VULTR_API_KEY"):
        return 0
    v = Vultr(os.environ["VULTR_API_KEY"])
    if st.get("instance_id"):
        inst = v.get_instance(st["instance_id"])
        print("instance:", "GONE" if not inst else
              f"{inst.get('label')} status={inst.get('status')} power={inst.get('power_status')} ip={inst.get('main_ip')}")
    if os.environ.get("VULTR_RESERVED_IP_ID"):
        rip = v.get_reserved_ip(os.environ["VULTR_RESERVED_IP_ID"])
        print(f"reserved IP: {rip.get('subnet')} attached to: {rip.get('instance_id') or '(nothing)'}")
    others = [i for i in v.list_instances() if i.get("label", "").startswith(LABEL_PREFIX)]
    print(f"livestack servers running: {len(others)}" + "".join(f"\n  {i['label']} {i['id']} since {i.get('date_created')}" for i in others))
    return 0


def _session_host() -> str:
    st = load_state()
    host = st.get("main_ip") or st.get("reserved_ip")
    if not host:
        raise SystemExit("no active session")
    return host


def cmd_health(a: argparse.Namespace) -> int:
    return ssh(_session_host(), "/opt/livestack/server/healthcheck.sh", check=False, timeout=300).returncode


def cmd_fetch_recordings(a: argparse.Namespace) -> int:
    host, dest = _session_host(), Path(a.dest)
    dest.mkdir(parents=True, exist_ok=True)
    log.info("downloading /var/lib/livestack/recordings from %s to %s", host, dest)
    r = subprocess.run(["scp", *ssh_opts(), "-r", f"{ssh_target(host)}:/var/lib/livestack/recordings", str(dest)])
    if r.returncode == 0:
        log.info("done. Encode + upload with: python control/replay.py run <file.mp4> --session-id <id>")
    return r.returncode


def cmd_orphans(a: argparse.Namespace) -> int:
    v = Vultr(require("VULTR_API_KEY")[0])
    max_h = a.max_hours if a.max_hours is not None else float(os.environ.get("MAX_SESSION_HOURS", "4"))
    now = dt.datetime.now(dt.timezone.utc)
    overdue = []
    for inst in v.list_instances():
        if not inst.get("label", "").startswith(LABEL_PREFIX):
            continue
        age_h = (now - dt.datetime.fromisoformat(inst["date_created"])).total_seconds() / 3600
        flag = "OVERDUE" if age_h > max_h else "ok"
        print(f"{flag:8} {inst['label']:32} {inst['id']}  {age_h:5.1f} h  {inst.get('main_ip')}")
        if age_h > max_h:
            overdue.append(inst)
    if not overdue:
        print(f"no livestack servers older than {max_h} h")
        return 0
    alert(f"{len(overdue)} livestack server(s) running longer than {max_h} h: "
          + ", ".join(i["label"] for i in overdue) + " - they bill every hour", "error")
    if a.destroy:
        for inst in overdue:
            confirm(f"Destroy overdue {inst['label']}? (any replay not yet uploaded is lost)", inst["label"], a.yes)
            destroy_instance(v, inst)
        return 0
    return 1


def cmd_snapshot(a: argparse.Namespace) -> int:
    v = Vultr(require("VULTR_API_KEY")[0])
    if not v.get_instance(a.instance_id):
        raise SystemExit(f"instance {a.instance_id} not found")
    desc = a.description or f"livestack golden {dt.date.today().isoformat()}"
    snap = v.create_snapshot(a.instance_id, desc)
    log.info("snapshot %s requested (%s); waiting until complete (can take 10-30 min)...", snap["id"], desc)
    wait_until(lambda: (v.get_snapshot(snap["id"]) or {}).get("status") == "complete", a.timeout, 30, "snapshot")
    print(f"\nSnapshot complete. Put this in .env:\nVULTR_SNAPSHOT_ID={snap['id']}")
    return 0


COMMANDS = {"start": cmd_start, "stop": cmd_stop, "status": cmd_status, "health": cmd_health,
            "fetch-recordings": cmd_fetch_recordings, "orphans": cmd_orphans, "snapshot": cmd_snapshot}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--env-file", help="settings file (default livestack/.env)")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("start", help="create + boot + health-check the session server")
    s.add_argument("--session", help="session id (default YYYYMMDD-HHMM); label becomes livestack-<id>")
    s.add_argument("--boot-timeout", type=float, default=900, help="seconds to wait for active/SSH")
    s.add_argument("--boot-script-timeout", type=float, default=900)
    s.add_argument("--steal-ip", action="store_true", help="move the reserved IP even if attached to another instance")
    s.add_argument("--destroy-on-failure", action="store_true", help="destroy the server if start errors")

    s = sub.add_parser("stop", help="verify replay in R2, then destroy the server")
    s.add_argument("--instance-id")
    s.add_argument("--replay-key", help="R2 object key of the replay")
    s.add_argument("--replay-file", help="local MP4 that was uploaded (its size must match)")
    s.add_argument("--replay-size", type=int, help="expected object size in bytes")
    s.add_argument("--skip-replay-check", action="store_true", help="destroy without a verified replay (asks to confirm)")
    s.add_argument("--yes", action="store_true", help="do not ask for confirmation")

    sub.add_parser("status", help="show session state and cloud resources")
    sub.add_parser("health", help="re-run the on-server health check")

    s = sub.add_parser("fetch-recordings", help="download the server-side backup recordings")
    s.add_argument("--dest", default=str(ROOT / "recordings"))

    s = sub.add_parser("orphans", help="find livestack servers running too long")
    s.add_argument("--max-hours", type=float)
    s.add_argument("--destroy", action="store_true")
    s.add_argument("--yes", action="store_true")

    s = sub.add_parser("snapshot", help="create the golden snapshot from an instance")
    s.add_argument("--instance-id", required=True)
    s.add_argument("--description")
    s.add_argument("--timeout", type=float, default=3600)
    return p


def main(argv: list[str] | None = None) -> int:
    a = build_parser().parse_args(argv)
    setup_logging(a.verbose)
    load_env(a.env_file)
    try:
        return COMMANDS[a.cmd](a) or 0
    except KeyboardInterrupt:
        log.error("interrupted - progress saved in %s; run the same command again to resume", STATE_FILE)
        return 130
    except (VultrError, TransientError, TimeoutError, RuntimeError, subprocess.TimeoutExpired) as e:
        log.error("%s", e)
        alert(f"`livestack.py {a.cmd}` failed: {e}", "error")
        return 1


if __name__ == "__main__":
    sys.exit(main())
