"""CMS bridge: runs the livestack lifecycle for a `live_sessions` row (Postgres) so the
whole thing can be driven from the Directus CMS.

It does not change or replace livestack.py. It calls the same functions (create the
server from the snapshot, attach the reserved IP, boot over SSH, destroy) and records
progress, monitoring data and a timeline in the database that Directus shows.

  run_start    create + boot + health-check the server          (long: ~5 min, runs in a Job)
  run_end      optionally save a server replay, then destroy server (long, runs in a Job)
  monitor_session / monitor_all   refresh health + metrics     (short, runs in the service)

Safety rules carried over from livestack:
  * a server is destroyed only after the replay is verified in R2 (or the operator
    explicitly chose "destroy without replay");
  * only servers labelled livestack-* are ever destroyed;
  * a failed step never destroys anything.
"""
from __future__ import annotations

import argparse
import datetime as dt
import decimal
import hashlib
import logging
import os
import re
import secrets
import ssl
import subprocess
import sys
import threading
import urllib.parse
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import livestack  # noqa: E402
import probe  # noqa: E402
from common import alert, log  # noqa: E402

ACTIVE = ("starting", "ready", "live", "ending")
START_REQUIRED = ("VULTR_API_KEY", "VULTR_REGION", "VULTR_PLAN", "VULTR_SNAPSHOT_ID", "VULTR_RESERVED_IP_ID",
                  "ORIGIN_HOST", "INGEST_HOST", "LIVE_HOST", "PUBLISH_USER", "PUBLISH_PASS", "STREAMER_IPS")
STAGE_TEXT = {"created": "Server created", "active": "Server running", "ip_attached": "Reserved IP attached",
              "ssh": "SSH reachable", "boot_done": "Boot script finished"}
STREAM_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{3,64}$")
CODE_RE = re.compile(r"^[A-Za-z0-9-]{1,40}$")
SECRET_NAME_RE = re.compile(r"(PASS|SECRET|TOKEN|API_KEY|ACCESS_KEY)", re.I)


def now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def prepare_environment() -> None:
    """Cloud Run entry setup, shared by the service and the job.

      LIVE_ENV_FILE    mounted secret holding the same livestack/.env you use on your PC
                       (real environment variables win, so the Secret Manager R2/database
                       settings override whatever the file says)
      SSH_KEY_SOURCE   mounted secret holding the private key; ssh refuses keys that are
                       group/world readable or lack a final newline, so a private copy is made
    """
    from common import load_env
    env_file = os.environ.get("LIVE_ENV_FILE")
    if env_file and Path(env_file).exists():
        load_env(env_file)
    src = os.environ.get("SSH_KEY_SOURCE")
    if src and Path(src).exists():
        dest_dir = Path(os.environ.get("SSH_KEY_DIR", "/tmp/livestack-ssh"))
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / "key"
        text = Path(src).read_text(encoding="utf-8").replace("\r\n", "\n")
        dest.write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")
        os.chmod(dest, 0o600)
        os.environ["SSH_KEY_PATH"] = str(dest)


def redact(text: str) -> str:
    """Remove any configured secret value from text before it is stored or shown."""
    text = str(text)
    for name, value in os.environ.items():
        if SECRET_NAME_RE.search(name) and len(value) >= 8 and value in text:
            text = text.replace(value, "***")
    return re.sub(r"(pass|password|token|key)=[^&\s'\"]+", r"\1=***", text, flags=re.I)


def missing(names=START_REQUIRED) -> list[str]:
    return [n for n in names if not os.environ.get(n)]


# =========================================================================== database
class Store:
    """The few queries the control service needs (pg8000, TLS verified)."""

    UPDATABLE = {
        "status", "stage", "error", "stream_name", "obs_server", "instance_id", "main_ip", "reserved_ip",
        "started_at", "ready_at", "live_since", "ended_at", "boot_seconds", "health_ok", "health_detail",
        "ingest_live", "cdn_ok", "encoder_speed", "playlist_age_seconds", "disk_percent", "students_watching",
        "last_checked_at", "replay_status", "replay_key", "replay_size_bytes", "replay_sha256",
        "video_asset_id", "job_execution", "requested_by",
        "peak_students", "ingest_kbps", "ingest_bytes", "server_log",
    }

    def __init__(self, url: str, ca_pem: str | None = None):
        self.url, self.ca, self._conn = url, ca_pem, None
        self._lock = threading.RLock()   # one connection, shared by the HTTP server's threads

    @classmethod
    def from_env(cls) -> "Store":
        url = os.environ.get("LMS_DATABASE_URL")
        if not url:
            raise SystemExit("LMS_DATABASE_URL is not set")
        ca = os.environ.get("LMS_DATABASE_CA_PEM") or None
        return cls(url, ca)

    def _connect(self):
        import pg8000.dbapi
        u = urllib.parse.urlparse(self.url)
        if (u.port or 5432) == 6543:
            raise RuntimeError("port 6543 (transaction pooler) is not supported; use the session pooler on 5432")
        local = u.hostname in ("localhost", "127.0.0.1", "::1")
        if not self.ca and not local:
            raise RuntimeError("refusing a database connection without a CA certificate (LMS_DATABASE_CA_PEM)")
        ctx = ssl.create_default_context(cadata=self.ca) if self.ca else None
        conn = pg8000.dbapi.connect(
            user=urllib.parse.unquote(u.username or ""), password=urllib.parse.unquote(u.password or ""),
            host=u.hostname, port=u.port or 5432, database=(u.path or "/postgres").lstrip("/") or "postgres",
            ssl_context=ctx, timeout=30)
        conn.autocommit = True
        return conn

    def q(self, sql: str, params: tuple | list = ()) -> list[dict]:
        with self._lock:
            return self._q(sql, params)

    def _q(self, sql: str, params: tuple | list = ()) -> list[dict]:
        for attempt in (1, 2):
            try:
                if self._conn is None:
                    self._conn = self._connect()
                cur = self._conn.cursor()
                cur.execute(sql, params)
                if not cur.description:
                    return []
                cols = [d[0] for d in cur.description]
                return [{c: self._clean(v) for c, v in zip(cols, row)} for row in cur.fetchall()]
            except Exception as e:  # noqa: BLE001
                name = type(e).__name__
                if attempt == 1 and name in ("InterfaceError", "OperationalError", "OSError", "ConnectionResetError"):
                    try:
                        self._conn and self._conn.close()
                    except Exception:  # noqa: BLE001
                        pass
                    self._conn = None
                    continue
                raise
        return []

    @staticmethod
    def _clean(v):
        if isinstance(v, uuid.UUID):
            return str(v)
        if isinstance(v, decimal.Decimal):
            return float(v)
        return v

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            finally:
                self._conn = None

    # ---- sessions
    def get(self, sid: str) -> dict | None:
        rows = self.q("select * from public.live_sessions where id = %s", (sid,))
        return rows[0] if rows else None

    def update(self, sid: str, **fields) -> None:
        bad = set(fields) - self.UPDATABLE
        if bad:
            raise ValueError(f"cannot update {sorted(bad)}")
        if not fields:
            return
        sets = ", ".join(f"{k} = %s" for k in fields)
        self.q(f"update public.live_sessions set {sets} where id = %s", (*fields.values(), sid))

    def claim(self, sid: str, from_statuses: tuple, to: str, **fields) -> tuple[dict | None, str]:
        """Atomically move a session to `to` if it is in one of `from_statuses`.
        Returns (row, "") or (None, reason). The unique index makes a second running session impossible."""
        bad = set(fields) - self.UPDATABLE
        if bad:
            raise ValueError(f"cannot update {sorted(bad)}")
        sets = "".join(f", {k} = %s" for k in fields)
        try:
            rows = self.q(f"update public.live_sessions set status = %s{sets} where id = %s and status = any(%s) returning *",
                          (to, *fields.values(), sid, list(from_statuses)))
        except Exception as e:  # noqa: BLE001
            if "live_sessions_one_running" in str(e) or "23505" in str(e):
                return None, "busy"
            raise
        return (rows[0], "") if rows else (None, "status")

    def running_elsewhere(self, sid: str) -> dict | None:
        rows = self.q("select id, title, status from public.live_sessions where id <> %s and status = any(%s) limit 1",
                      (sid, list(ACTIVE)))
        return rows[0] if rows else None

    def monitored(self) -> list[dict]:
        return self.q("select * from public.live_sessions where status in ('ready','live') "
                      "or (status = 'failed' and instance_id is not null and ended_at is null)")

    def event(self, sid: str, level: str, source: str, message: str) -> None:
        try:
            self.q("insert into public.live_events (session_id, level, source, message) values (%s, %s, %s, %s)",
                   (sid, level, source, redact(message)[:1900]))
        except Exception as e:  # noqa: BLE001  (a log write must never break the operation)
            log.error("could not write event: %s", e)

    def add_metric(self, sid: str, **m) -> None:
        cols = ("health_ok", "ingest_live", "students_watching", "encoder_speed", "ingest_kbps", "playlist_age_seconds", "disk_percent")
        self.q("insert into public.live_metrics (session_id, health_ok, ingest_live, students_watching, encoder_speed, ingest_kbps, "
               "playlist_age_seconds, disk_percent) values (%s,%s,%s,%s,%s,%s,%s,%s)", (sid, *[m.get(c) for c in cols]))

    def purge_old_metrics(self, days: int = 30) -> None:
        self.q("delete from public.live_metrics where at < now() - make_interval(days => %s)", (days,))

    def put_obs_key(self, sid: str, key: str) -> None:
        self.q("insert into public.live_session_secrets (session_id, obs_stream_key) values (%s, %s) "
               "on conflict (session_id) do update set obs_stream_key = excluded.obs_stream_key", (sid, key))

    # ---- classes / students / recordings
    def get_class(self, cid: str) -> dict | None:
        rows = self.q("select id, title, status, stream_path, scheduled_start, actual_start from public.classes where id = %s", (cid,))
        return rows[0] if rows else None

    def ensure_stream_path(self, cid: str) -> str:
        cls = self.get_class(cid)
        if not cls:
            raise RuntimeError("the class linked to this session no longer exists")
        if cls["stream_path"]:
            return cls["stream_path"]
        path = "live/" + secrets.token_urlsafe(12)
        self.q("update public.classes set stream_path = %s where id = %s and stream_path is null", (path, cid))
        return self.get_class(cid)["stream_path"]

    def set_class_live(self, cid: str) -> None:
        self.q("update public.classes set status = 'live', actual_start = coalesce(actual_start, now()), "
               "scheduled_start = coalesce(scheduled_start, now()) where id = %s and status in ('scheduled','live','draft','processing')", (cid,))

    def set_class_ended(self, cid: str, published: bool) -> None:
        self.q("update public.classes set status = %s, actual_end = now(), "
               "actual_start = coalesce(actual_start, now()) where id = %s and status in ('live','scheduled','processing','draft')",
               ("published" if published else "processing", cid))

    def watching(self, cid: str, within_seconds: int = 90) -> int:
        rows = self.q("select count(*)::int n from public.class_attendance where class_id = %s and mode = 'live' "
                      "and last_seen_at > now() - make_interval(secs => %s)", (cid, within_seconds))
        return rows[0]["n"]

    def upsert_video_asset(self, existing_id: str | None, **v) -> str:
        cols = ("class_id", "title", "source_key", "duration_seconds", "size_bytes", "sha256")
        if existing_id:
            self.q("update public.video_assets set class_id=%s, title=%s, source_key=%s, duration_seconds=%s, size_bytes=%s, "
                   "sha256=%s, provider='r2', kind='recording', status='ready', error=null where id = %s",
                   (*[v.get(c) for c in cols], existing_id))
            return existing_id
        rows = self.q("insert into public.video_assets (class_id, title, source_key, duration_seconds, size_bytes, sha256, "
                      "provider, kind, protection, status) values (%s,%s,%s,%s,%s,%s,'r2','recording','signed_url','ready') returning id",
                      tuple(v.get(c) for c in cols))
        return rows[0]["id"]


# =========================================================================== helpers
class StageLog(logging.Handler):
    """Copies warnings/errors from the livestack logger into the session timeline."""

    def __init__(self, store: Store, sid: str, source: str = "job"):
        super().__init__(level=logging.WARNING)
        self.store, self.sid, self.source, self.count = store, sid, source, 0

    def emit(self, record: logging.LogRecord) -> None:
        if self.count >= 40:
            return
        self.count += 1
        self.store.event(self.sid, "error" if record.levelno >= logging.ERROR else "warning", self.source, record.getMessage())


def hook_state_saving(store: Store, sid: str):
    """Makes livestack.save_state also record progress in the database. Returns an undo function."""
    original = livestack.save_state
    seen: set[str] = set()

    def save(st: dict) -> None:
        original(st)
        try:
            fields = {k: st[k] for k in ("instance_id", "main_ip", "reserved_ip") if st.get(k)}
            stage = st.get("stage")
            if stage:
                fields["stage"] = stage
            if st.get("stage_error"):
                fields["error"] = redact(st["stage_error"])[:1000]
            store.update(sid, **fields)
            if stage in STAGE_TEXT and stage not in seen:
                seen.add(stage)
                took = (st.get("timings") or {}).get(stage)
                store.event(sid, "info", "job", f"{STAGE_TEXT[stage]}" + (f" ({took}s)" if took is not None else ""))
        except Exception as e:  # noqa: BLE001
            log.warning("could not record progress: %s", e)

    livestack.save_state = save
    return lambda: setattr(livestack, "save_state", original)


def vultr() -> "livestack.Vultr":
    return livestack.Vultr(os.environ["VULTR_API_KEY"])


def stream_for(store: Store, session: dict) -> tuple[str, str]:
    """(stream path 'live/<name>', name). A linked class owns the path so students' playback matches."""
    if session.get("class_id"):
        path = store.ensure_stream_path(session["class_id"])
    elif session.get("stream_name"):
        # Older/backend-created rows may store either the bare name or the full
        # MediaMTX path. Normalize both forms so OBS and monitoring use one path.
        name = str(session["stream_name"]).removeprefix("live/")
        path = "live/" + name
    else:
        path = "live/" + secrets.token_urlsafe(12)
    name = path.split("/", 1)[1]
    if not STREAM_NAME_RE.match(name):
        raise RuntimeError(f"invalid stream name {name!r}")
    return path, name


# =========================================================================== start
def run_start(store: Store, sid: str, boot_timeout: float = 900) -> int:
    """Create + boot the server for session `sid` (already claimed as 'starting'). Returns an exit code."""
    row = store.get(sid)
    if not row:
        raise SystemExit(f"session {sid} not found")
    code = row["session_code"]
    problems = missing()
    if problems:
        msg = "Livestream settings are missing: " + ", ".join(problems) + ". Fill them in livestack/.env and run deploy-livestream again."
        store.update(sid, status="failed", error=msg)
        store.event(sid, "error", "job", msg)
        return 1

    handler = StageLog(store, sid)
    logging.getLogger("livestack").addHandler(handler)
    undo = hook_state_saving(store, sid)
    try:
        path, name = stream_for(store, row)
        store.update(sid, stream_name=name, obs_server=f"rtmp://{os.environ['INGEST_HOST']}/live", started_at=row.get("started_at") or now())
        store.event(sid, "info", "job", f"Starting server for {code} (stream {path})")
        ns = argparse.Namespace(session=code, boot_timeout=boot_timeout, boot_script_timeout=900, steal_ip=False, destroy_on_failure=False)
        try:
            rc = livestack.cmd_start(ns)
            err = None if rc == 0 else "The server is running but the on-server health check failed. Check the timeline, then press Start again to retry, or End/Destroy the session."
        except SystemExit as e:
            rc, err = 1, str(e.code)
        except Exception as e:  # noqa: BLE001
            rc, err = 1, f"{type(e).__name__}: {e}"
        st = livestack.load_state()
        if rc == 0 and st.get("stage") == "ready":
            key = f"{name}?user={os.environ['PUBLISH_USER']}&pass={os.environ['PUBLISH_PASS']}"
            store.put_obs_key(sid, key)
            store.update(sid, status="ready", stage="ready", error=None, ready_at=now(),
                         boot_seconds=(st.get("timings") or {}).get("boot_done"),
                         instance_id=st.get("instance_id"), main_ip=st.get("main_ip"), reserved_ip=st.get("reserved_ip"))
            store.event(sid, "info", "job", "READY: the streamer can go live. OBS server and key are on this session.")
            return 0
        store.update(sid, status="failed", error=redact(err or "start did not complete")[:1000])
        store.event(sid, "error", "job", err or "start did not complete")
        return rc or 1
    finally:
        undo()
        logging.getLogger("livestack").removeHandler(handler)


# =========================================================================== monitoring
REMOTE_STATUS = r"""
echo '###paths'; curl -s -m 5 http://127.0.0.1:9997/v3/paths/list 2>/dev/null || true; echo
echo '###speed'; grep -h 'speed=' /run/livestack/progress-live_* 2>/dev/null | tail -1 || true
echo '###disk'; df -P /var/lib/livestack 2>/dev/null | awk 'NR==2{print $5}'
echo '###log'; journalctl -u mediamtx -u livestack-monitor --no-pager -n 25 -o short-iso 2>/dev/null | cut -c1-200 || true
"""


def parse_status(text: str, stream_path: str) -> dict:
    """Parse the output of REMOTE_STATUS. Pure function (unit-tested)."""
    import json
    sections: dict[str, str] = {}
    cur = None
    for line in text.splitlines():
        if line.startswith("###"):
            cur = line[3:].strip()
            sections[cur] = ""
        elif cur:
            sections[cur] += line + "\n"
    out: dict = {"ingest_live": False, "encoder_speed": None, "disk_percent": None, "ingest_bytes": None,
                 "server_log": (sections.get("log") or "").strip()}
    try:
        items = json.loads(sections.get("paths", "") or "{}").get("items", [])
        mine = [i for i in items if i.get("name") == stream_path and i.get("ready")]
        out["ingest_live"] = bool(mine)
        if mine and isinstance(mine[0].get("bytesReceived"), int):
            out["ingest_bytes"] = mine[0]["bytesReceived"]
    except ValueError:
        pass
    m = re.search(r"speed=\s*([0-9.]+)x", sections.get("speed", ""))
    if m:
        out["encoder_speed"] = float(m.group(1))
    m = re.search(r"(\d+)%", sections.get("disk", ""))
    if m:
        out["disk_percent"] = int(m.group(1))
    return out


def playlist_age_seconds(master_url: str, now_ts: float | None = None) -> tuple[int | None, str]:
    """Age of the newest 720p segment (segment names are epoch seconds). (age, error)"""
    import time
    try:
        base = master_url.rsplit("/", 1)[0]
        text = probe.fetch_text(f"{base}/720p/index.m3u8", timeout=8)
        segs = probe.parse_media(text).segments
        if not segs:
            return None, "no segments in the playlist"
        m = re.search(r"(\d{9,})", segs[-1])
        if not m:
            return None, "unrecognised segment name"
        return max(0, int((now_ts or time.time()) - int(m.group(1)))), ""
    except Exception as e:  # noqa: BLE001
        return None, str(e)


def monitor_session(store: Store, row: dict) -> dict:
    """Refresh health + metrics for one running session and apply status transitions."""
    sid, status = row["id"], row["status"]
    detail: list[str] = []
    ok = True
    fields: dict = {"last_checked_at": now()}

    inst = None
    if row.get("instance_id"):
        try:
            inst = vultr().get_instance(row["instance_id"])
        except Exception as e:  # noqa: BLE001
            detail.append(f"cloud API unreachable ({e})")
            ok = False
        else:
            if inst is None and status in ("ready", "live"):
                msg = "The server no longer exists at the cloud provider (deleted outside the CMS?)."
                store.update(sid, status="failed", error=msg, health_ok=False, health_detail=msg, last_checked_at=now())
                store.event(sid, "error", "monitor", msg)
                alert(f"session {row['session_code']}: {msg}", "error")
                return {"id": sid, "status": "failed"}
            if inst is not None:
                detail.append(f"server {inst.get('status')}/{inst.get('power_status')}")
                if inst.get("power_status") != "running":
                    ok = False

    stream_path = ("live/" + row["stream_name"]) if row.get("stream_name") else None
    origin, live_host = os.environ.get("ORIGIN_HOST"), os.environ.get("LIVE_HOST")
    if origin:
        o_ok, o_msg = livestack.https_ok(f"https://{origin}/healthz")
        detail.append("origin OK" if o_ok else f"origin DOWN ({o_msg[:80]})")
        ok = ok and o_ok

    snap = {"ingest_live": None, "encoder_speed": None, "disk_percent": None, "ingest_bytes": None, "server_log": None}
    host = row.get("main_ip") or row.get("reserved_ip")
    if host and stream_path:
        try:
            r = livestack.ssh(host, "bash -s", input_text=REMOTE_STATUS, capture=True, check=False, timeout=25)
            if r.returncode == 0:
                snap = parse_status(r.stdout, stream_path)
            else:
                detail.append("cannot read server status over SSH")
                ok = False
        except Exception as e:  # noqa: BLE001
            detail.append(f"SSH failed ({str(e)[:80]})")
            ok = False
    fields.update(ingest_live=snap["ingest_live"], encoder_speed=snap["encoder_speed"], disk_percent=snap["disk_percent"],
                  ingest_bytes=snap["ingest_bytes"])
    if snap["server_log"] is not None:
        fields["server_log"] = redact(snap["server_log"])[-4000:]
    kbps = None
    prev_bytes, prev_at = row.get("ingest_bytes"), row.get("last_checked_at")
    if snap["ingest_bytes"] is not None and prev_bytes is not None and prev_at is not None and snap["ingest_bytes"] >= prev_bytes:
        secs = (fields["last_checked_at"] - prev_at).total_seconds()
        if secs > 0:
            kbps = int((snap["ingest_bytes"] - prev_bytes) * 8 / 1000 / secs)
    fields["ingest_kbps"] = kbps if snap["ingest_live"] else (0 if snap["ingest_live"] is False else None)
    if snap["ingest_live"]:
        detail.append("stream live" + (f" at {kbps} kbps" if kbps is not None else ""))
    elif snap["ingest_live"] is False:
        detail.append("no stream from OBS")
    if snap["encoder_speed"] is not None:
        detail.append(f"encoder {snap['encoder_speed']:.2f}x")
        if snap["encoder_speed"] < 0.97 and snap["ingest_live"]:
            ok = False
    if snap["disk_percent"] is not None:
        detail.append(f"disk {snap['disk_percent']}%")
        if snap["disk_percent"] >= 85:
            ok = False

    cdn_ok, age = None, None
    if snap["ingest_live"] and live_host and stream_path:
        age, err = playlist_age_seconds(f"https://{live_host}/hls/{stream_path}/master.m3u8")
        cdn_ok = age is not None and age <= 20
        detail.append(f"viewer playlist {age}s old" if age is not None else f"viewer playlist unreachable ({err[:60]})")
        ok = ok and cdn_ok
    fields.update(cdn_ok=cdn_ok, playlist_age_seconds=age)
    if row.get("class_id"):
        try:
            fields["students_watching"] = store.watching(row["class_id"])
            fields["peak_students"] = max(fields["students_watching"], row.get("peak_students") or 0)
        except Exception as e:  # noqa: BLE001
            log.warning("student count failed: %s", e)

    # transitions
    if status == "ready" and snap["ingest_live"]:
        fields.update(status="live", live_since=now())
        store.event(sid, "info", "monitor", "Stream started: OBS is publishing.")
        if row.get("class_id"):
            store.set_class_live(row["class_id"])
    elif status == "live" and snap["ingest_live"] is False:
        fields.update(status="ready")
        store.event(sid, "warning", "monitor", "Stream stopped: OBS is no longer publishing (disconnected?).")
    if row.get("health_ok") is not None and bool(row["health_ok"]) != ok:
        store.event(sid, "info" if ok else "warning", "monitor", ("Healthy again: " if ok else "Problem: ") + "; ".join(detail))
        if not ok:
            alert(f"session {row['session_code']}: " + "; ".join(detail), "warning")
    fields.update(health_ok=ok, health_detail="; ".join(detail)[:500])

    started = row.get("started_at")
    if started and status in ("ready", "live") and (now() - started).total_seconds() > float(row.get("max_hours") or 4) * 3600:
        msg = f"Server has been running longer than {row.get('max_hours') or 4} h and is still billing. End the session if the class is over."
        last = store.q("select max(at) t from public.live_events where session_id = %s and message like 'Server has been running longer%%'", (sid,))
        if not last or not last[0]["t"] or (now() - last[0]["t"]).total_seconds() > 1800:
            store.event(sid, "warning", "monitor", msg)
            alert(f"session {row['session_code']}: {msg}", "error")
    store.update(sid, **fields)
    try:
        store.add_metric(sid, health_ok=ok, ingest_live=snap["ingest_live"], students_watching=fields.get("students_watching"),
                         encoder_speed=snap["encoder_speed"], ingest_kbps=fields["ingest_kbps"], playlist_age_seconds=age,
                         disk_percent=snap["disk_percent"])
    except Exception as e:  # noqa: BLE001  (a chart point must never break monitoring)
        log.warning("could not record metric: %s", e)
    return {"id": sid, "status": fields.get("status", status), "health_ok": ok, "detail": fields["health_detail"]}


def monitor_all(store: Store) -> list[dict]:
    out = []
    for row in store.monitored():
        try:
            out.append(monitor_session(store, row))
        except Exception as e:  # noqa: BLE001
            log.error("monitor %s failed: %s", row.get("session_code"), e)
            out.append({"id": row["id"], "error": str(e)})
    # servers at the cloud provider that no session knows about (forgotten, billing)
    try:
        known = {r["instance_id"] for r in store.q("select instance_id from public.live_sessions where instance_id is not null and ended_at is null")}
        stray = [i for i in vultr().list_instances() if i.get("label", "").startswith(livestack.LABEL_PREFIX) and i["id"] not in known]
        if stray:
            alert("livestack server(s) with no CMS session (still billing): " + ", ".join(i["label"] for i in stray), "error")
            out.append({"orphans": [i["label"] for i in stray]})
    except Exception as e:  # noqa: BLE001
        log.warning("orphan check skipped: %s", e)
    return out


# =========================================================================== end
class ReplayError(RuntimeError):
    pass


EXPORT_SCRIPT = r"""set -e
d=/var/lib/livestack/recordings/live/{name}
if ! ls "$d"/*.mp4 >/dev/null 2>&1; then echo NO_RECORDING; exit 3; fi
out=/var/lib/livestack/replay-{code}.mp4
if [ ! -s "$out" ]; then
  list=$(mktemp)
  for f in $(ls -1 "$d"/*.mp4 | sort); do echo "file '$f'" >> "$list"; done
  ffmpeg -nostdin -v error -f concat -safe 0 -i "$list" -c copy -movflags +faststart -y "$out.part" -f mp4
  mv "$out.part" "$out"
  rm -f "$list"
fi
echo SIZE=$(stat -c %s "$out")
echo SHA=$(sha256sum "$out" | cut -d' ' -f1)
echo DUR=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$out")
"""


class HashingReader:
    def __init__(self, f):
        self.f, self.sha, self.n = f, hashlib.sha256(), 0

    def read(self, size: int = -1) -> bytes:
        b = self.f.read(size)
        self.sha.update(b)
        self.n += len(b)
        return b


def export_replay(store: Store, row: dict, host: str) -> dict:
    """Remux the server's backup recording to one MP4, stream it to R2 and verify it. Raises ReplayError."""
    import replay
    sid, code, name = row["id"], row["session_code"], row.get("stream_name") or ""
    if not STREAM_NAME_RE.match(name) or not CODE_RE.match(code):
        raise ReplayError("session has no valid stream name; nothing to export")
    r = livestack.ssh(host, "bash -s", input_text=EXPORT_SCRIPT.format(name=name, code=code), capture=True, check=False, timeout=3600)
    if "NO_RECORDING" in (r.stdout or ""):
        raise ReplayError("the server has no recording of this stream (OBS never published, or backup recording is off)")
    vals = dict(re.findall(r"^(SIZE|SHA|DUR)=(\S*)$", r.stdout or "", re.M))
    if r.returncode != 0 or not vals.get("SIZE") or not re.fullmatch(r"[0-9a-f]{64}", vals.get("SHA", "")):
        raise ReplayError("could not prepare the replay on the server: " + redact((r.stderr or r.stdout or "").strip()[-300:]))
    size, sha = int(vals["SIZE"]), vals["SHA"]
    duration = int(float(vals["DUR"])) if re.fullmatch(r"[0-9.]+", vals.get("DUR", "")) else None
    if size < 1024:
        raise ReplayError(f"the recording is only {size} bytes; refusing to treat it as a replay")

    root = (os.environ.get("LMS_R2_ROOT") or "cms").strip("/")
    when = row.get("live_since") or row.get("ready_at") or now()
    rel = f"replays/{when:%Y}/{when:%m}/{code}.mp4"
    key = f"{root}/{rel}" if root else rel
    s3 = replay.s3_client()
    store.update(sid, replay_status="uploading", replay_key=rel, replay_size_bytes=size, replay_sha256=sha)

    existing = replay.head(s3, key)
    if existing and existing["ContentLength"] == size and (existing.get("Metadata") or {}).get("sha256") == sha:
        store.event(sid, "info", "job", "Replay already in R2 with the same checksum; skipping upload.")
    else:
        store.event(sid, "info", "job", f"Uploading replay ({size / 1e6:.0f} MB) to R2...")
        from boto3.s3.transfer import TransferConfig
        proc = subprocess.Popen(["ssh", *livestack.ssh_opts(), livestack.ssh_target(host), f"cat /var/lib/livestack/replay-{code}.mp4"],
                                stdout=subprocess.PIPE)
        reader = HashingReader(proc.stdout)
        try:
            s3.upload_fileobj(reader, replay.bucket(), key, ExtraArgs={"ContentType": "video/mp4", "Metadata": {"sha256": sha}},
                              Config=TransferConfig(multipart_threshold=32 * 1024 * 1024, multipart_chunksize=32 * 1024 * 1024, max_concurrency=2))
        finally:
            proc.stdout.close()
            proc.wait(timeout=60)
        if reader.n != size or reader.sha.hexdigest() != sha:
            s3.delete_object(Bucket=replay.bucket(), Key=key)
            raise ReplayError(f"upload was corrupted in transit ({reader.n} of {size} bytes); the partial file was removed")
    ok, msg = replay.verify_object(key, size, sha, s3=s3)
    if not ok:
        raise ReplayError(msg)
    asset_id = store.upsert_video_asset(row.get("video_asset_id"), class_id=row.get("class_id"), title=row["title"],
                                        source_key=rel, duration_seconds=duration, size_bytes=size, sha256=sha)
    store.update(sid, replay_status="verified", video_asset_id=asset_id)
    store.event(sid, "info", "job", f"Replay verified in R2 ({msg}). Students can watch it from the class page.")
    return {"key": rel, "size": size, "sha256": sha, "duration": duration, "video_asset_id": asset_id}


def run_end(store: Store, sid: str, mode: str = "save") -> int:
    """End a session (already claimed as 'ending'). mode 'save': export + verify the replay first.
    mode 'discard': destroy the server without a replay. Returns an exit code."""
    row = store.get(sid)
    if not row:
        raise SystemExit(f"session {sid} not found")
    handler = StageLog(store, sid)
    logging.getLogger("livestack").addHandler(handler)
    try:
        v = vultr()
        inst = v.get_instance(row["instance_id"]) if row.get("instance_id") else None
        published = False
        if inst is None:
            store.event(sid, "warning", "job", "The server is already gone; no recording can be exported.")
            store.update(sid, replay_status="none" if row["replay_status"] != "verified" else "verified")
            published = row["replay_status"] == "verified"
        elif mode == "save" and os.environ.get("RECORD_BACKUP", "no").lower() not in {"no", "false", "0", "off"}:
            host = row.get("main_ip") or inst.get("main_ip")
            try:
                export_replay(store, row, host)
                published = True
            except ReplayError as e:
                msg = f"Replay not saved, so the server was NOT destroyed: {e}"
                store.update(sid, status="failed", replay_status="failed", error=redact(msg)[:1000])
                store.event(sid, "error", "job", msg + " Fix it and press End again, or use Destroy server (discard replay).")
                alert(f"session {row['session_code']}: {msg}", "error")
                return 3
            except Exception as e:  # noqa: BLE001
                msg = f"Replay export failed ({type(e).__name__}: {e}); the server was NOT destroyed."
                store.update(sid, status="failed", replay_status="failed", error=redact(msg)[:1000])
                store.event(sid, "error", "job", msg)
                return 3
        else:
            store.update(sid, replay_status="skipped")
            if mode == "save":
                store.event(sid, "info", "job", "Server-side recording is disabled. Destroying the stream server; upload the OBS recording separately in the admin dashboard.")
            else:
                store.event(sid, "warning", "job", "Destroying the server WITHOUT saving a replay (the server's backup recording will be lost).")

        if inst is not None:
            store.event(sid, "info", "job", f"Destroying server {inst.get('label')} ...")
            livestack.destroy_instance(v, inst)
            rid, snap = os.environ.get("VULTR_RESERVED_IP_ID"), os.environ.get("VULTR_SNAPSHOT_ID")
            if rid:
                try:
                    v.get_reserved_ip(rid)
                except Exception as e:  # noqa: BLE001
                    alert(f"reserved IP check failed after ending {row['session_code']}: {e}", "error")
            if snap and not v.get_snapshot(snap):
                alert("golden snapshot not found after ending a session!", "error")
        store.update(sid, status="ended", stage="destroyed", ended_at=now(), error=None, ingest_live=False,
                     cdn_ok=None, health_ok=None, health_detail="Server destroyed", students_watching=None)
        if row.get("class_id"):
            store.set_class_ended(row["class_id"], published)
        store.event(sid, "info", "job", "Session ended." + ("" if published else " No replay was saved; the class was left in 'processing'."))
        return 0
    except (Exception, SystemExit) as e:  # noqa: BLE001  (livestack refuses with SystemExit, e.g. a non-livestack server)
        what = e.code if isinstance(e, SystemExit) else f"{type(e).__name__}: {e}"
        store.update(sid, status="failed", error=redact(str(what))[:1000])
        store.event(sid, "error", "job", f"End failed: {what}. The server may still be running.")
        raise
    finally:
        logging.getLogger("livestack").removeHandler(handler)
