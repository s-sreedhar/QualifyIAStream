"""Tests for the CMS bridge (control/cms_bridge.py) and the control service.

Real pieces: Postgres 16 in Docker with the real cms migrations applied, the real
livestack.py start logic, the real HTTP service.
Faked pieces: the Vultr API (stateful local server), SSH, HTTPS probes and R2.
Skipped automatically when Docker is not available.
"""
from __future__ import annotations

import hashlib
import io
import json
import shutil
import subprocess
import sys
import threading
import time
import types
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "control"))
import cms_bridge  # noqa: E402
import livestack  # noqa: E402
import probe  # noqa: E402
import replay  # noqa: E402
import service  # noqa: E402

MIGRATIONS = ROOT.parent.parent / "cms" / "db" / "migrations"
PASS = "supersecretpublishpass123"
ADMIN = str(uuid.uuid4())
OPERATOR = str(uuid.uuid4())
EDITOR = str(uuid.uuid4())

def _find_docker() -> str | None:
    found = shutil.which("docker")
    if found:
        return found
    for p in (r"C:\Program Files\Docker\Docker\resources\bin\docker.exe", "/usr/bin/docker", "/usr/local/bin/docker"):
        if Path(p).exists():
            return p
    return None


DOCKER = _find_docker()
pytestmark = pytest.mark.skipif(DOCKER is None, reason="docker is not installed")


# ---------------------------------------------------------------- real Postgres with the real migrations
def _docker(*args, stdin: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run([DOCKER, *args], input=stdin, capture_output=True, text=True, encoding="utf-8")


@pytest.fixture(scope="session")
def pg_url():
    if _docker("info").returncode != 0:
        pytest.skip("docker daemon is not running")
    name = f"livestack-test-{uuid.uuid4().hex[:8]}"
    r = _docker("run", "-d", "--rm", "--name", name, "-e", "POSTGRES_PASSWORD=test", "-p", "127.0.0.1::5432", "postgres:16")
    if r.returncode != 0:
        pytest.skip("cannot start postgres:16: " + r.stderr[:200])
    try:
        for _ in range(60):
            if _docker("exec", name, "pg_isready", "-U", "postgres").returncode == 0 and \
               _docker("exec", name, "psql", "-U", "postgres", "-tAc", "select 1").stdout.strip() == "1":
                break
            time.sleep(1)
        port = _docker("port", name, "5432/tcp").stdout.strip().splitlines()[0].rsplit(":", 1)[1]
        stub = """
          create table public.directus_users (id uuid primary key default gen_random_uuid(), email text, role uuid, status text default 'active');
          create table public.directus_files (id uuid primary key default gen_random_uuid(), filename_disk text);
          create table public.directus_policies (id uuid primary key default gen_random_uuid(), name text, admin_access boolean default false);
          create table public.directus_access (id uuid primary key default gen_random_uuid(), role uuid, "user" uuid, policy uuid);
        """
        r = _docker("exec", "-i", name, "psql", "-U", "postgres", "-v", "ON_ERROR_STOP=1", "-q", stdin=stub)
        assert r.returncode == 0, r.stderr
        for f in sorted(MIGRATIONS.glob("*.sql")):
            r = _docker("exec", "-i", name, "psql", "-U", "postgres", "-v", "ON_ERROR_STOP=1", "-q", "-1", stdin=f.read_text(encoding="utf-8"))
            assert r.returncode == 0, f"{f.name}: {r.stderr}"
        yield f"postgres://postgres:test@127.0.0.1:{port}/postgres"
    finally:
        _docker("rm", "-f", name)


@pytest.fixture()
def store(pg_url):
    s = cms_bridge.Store(pg_url)
    s.q("truncate public.live_sessions, public.classes, public.video_assets, public.class_attendance, public.users, "
        "public.directus_users, public.directus_policies, public.directus_access cascade")
    admin_policy, op_policy = str(uuid.uuid4()), str(uuid.uuid4())
    s.q("insert into public.directus_policies (id, name, admin_access) values (%s,'Administrator',true), (%s,'Stream Operator',false)", (admin_policy, op_policy))
    editor_policy = str(uuid.uuid4())
    s.q("insert into public.directus_policies (id, name) values (%s,'LMS Editor')", (editor_policy,))
    roles = {k: str(uuid.uuid4()) for k in ("admin", "op", "editor")}
    for uid, role in ((ADMIN, "admin"), (OPERATOR, "op"), (EDITOR, "editor")):
        s.q("insert into public.directus_users (id, role) values (%s, %s)", (uid, roles[role]))
    for role, pol in (("admin", admin_policy), ("op", op_policy), ("editor", editor_policy)):
        s.q('insert into public.directus_access (role, policy) values (%s, %s)', (roles[role], pol))
    yield s
    s.close()


# ---------------------------------------------------------------- fake Vultr (stateful)
class FakeVultr(BaseHTTPRequestHandler):
    state: dict = {}

    def log_message(self, *a):
        pass

    def _send(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):  # noqa: N802
        s, path = self.state, self.path.split("?")[0]
        if path == "/instances":
            q = dict(p.split("=", 1) for p in self.path.split("?", 1)[1].split("&")) if "?" in self.path else {}
            items = [i for i in s["instances"].values() if "label" not in q or i["label"] == q["label"]]
            return self._send(200, {"instances": items, "meta": {"links": {"next": ""}}})
        if path.startswith("/instances/"):
            i = s["instances"].get(path.rsplit("/", 1)[1])
            return self._send(200, {"instance": i}) if i else self._send(404, {"error": "not found"})
        if path.startswith("/reserved-ips/"):
            return self._send(200, {"reserved_ip": s["rip"]})
        if path.startswith("/snapshots/"):
            return self._send(200, {"snapshot": {"id": "snap-1", "description": "golden", "status": "complete"}})
        self._send(404, {})

    def do_POST(self):  # noqa: N802
        s, body = self.state, self._body()
        if self.path == "/instances":
            iid = f"inst-{len(s['instances']) + 1}"
            s["instances"][iid] = {"id": iid, "label": body["label"], "status": "active", "power_status": "running",
                                   "main_ip": "203.0.113.7", "date_created": "2026-11-12T01:00:00+00:00"}
            s["created"].append(body)
            return self._send(202, {"instance": s["instances"][iid]})
        if self.path.endswith("/attach"):
            s["rip"]["instance_id"] = body["instance_id"]
            return self._send(204, {})
        if self.path.endswith("/detach"):
            s["rip"]["instance_id"] = ""
            return self._send(204, {})
        self._send(404, {})

    def do_DELETE(self):  # noqa: N802
        iid = self.path.rsplit("/", 1)[1]
        if iid in self.state["instances"]:
            del self.state["instances"][iid]
            self.state["deleted"].append(iid)
            return self._send(204, {})
        self._send(404, {})


@pytest.fixture()
def vultr_srv():
    FakeVultr.state = {"instances": {}, "rip": {"id": "rip-1", "subnet": "198.51.100.9", "instance_id": ""}, "created": [], "deleted": []}
    srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeVultr)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield FakeVultr.state, f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


# ---------------------------------------------------------------- fake SSH / HTTPS / R2
RECORDING = b"\x00\x00\x00\x18ftypmp42" + b"v" * 5000
RECORDING_SHA = hashlib.sha256(RECORDING).hexdigest()


class World:
    """Everything outside the database and Vultr API, with switches for failure cases."""

    def __init__(self):
        self.boot_rc = 0
        self.ingest_live = False
        self.encoder_speed = 1.0
        self.recording = True
        self.truncate_upload = False
        self.origin_ok = True
        self.ssh_ok = True
        self.segment_age = 4
        self.bytes_received = 0
        self.log = "mediamtx: ok"
        self.uploaded: dict[str, dict] = {}
        self.deleted_keys: list[str] = []
        self.ssh_calls: list[str] = []

    # livestack.ssh replacement
    def ssh(self, host, remote_cmd, input_text=None, timeout=900, check=True, capture=False):
        self.ssh_calls.append(remote_cmd)
        out, rc = "", 0
        if remote_cmd == "/opt/livestack/server/boot.sh":
            rc = self.boot_rc
        elif remote_cmd == "bash -s" and input_text and "###paths" in input_text:
            if not self.ssh_ok:
                rc = 255
            else:
                paths = {"items": [{"name": "live/" + self.stream, "ready": True, "bytesReceived": self.bytes_received}]} if self.ingest_live else {"items": []}
                out = f"###paths\n{json.dumps(paths)}\n###speed\nspeed={self.encoder_speed:.2f}x\n###disk\n 7%\n###log\n{self.log}\n"
        elif remote_cmd == "bash -s" and input_text and "ffmpeg" in input_text:
            out = "NO_RECORDING\n" if not self.recording else f"SIZE={len(RECORDING)}\nSHA={RECORDING_SHA}\nDUR=3601.5\n"
            rc = 0 if self.recording else 3
        return types.SimpleNamespace(returncode=rc, stdout=out, stderr="")

    stream = "stream1"

    # boto3 client stand-in
    class S3:
        def __init__(self, w):
            self.w = w

        def head_object(self, Bucket, Key):
            if Key not in self.w.uploaded:
                from botocore.exceptions import ClientError
                raise ClientError({"Error": {"Code": "404"}}, "HeadObject")
            o = self.w.uploaded[Key]
            return {"ContentLength": len(o["data"]), "Metadata": o["meta"], "ContentType": "video/mp4"}

        def upload_fileobj(self, reader, Bucket, Key, ExtraArgs=None, Config=None):
            data = reader.read(-1) if hasattr(reader, "read") else b""
            self.w.uploaded[Key] = {"data": data, "meta": (ExtraArgs or {}).get("Metadata", {})}

        def delete_object(self, Bucket, Key):
            self.w.uploaded.pop(Key, None)
            self.w.deleted_keys.append(Key)

    def popen(self, cmd, stdout=None, **kw):
        data = RECORDING[:100] if self.truncate_upload else RECORDING
        return types.SimpleNamespace(stdout=io.BytesIO(data), wait=lambda timeout=None: 0)

    def fetch_text(self, url, timeout=10):
        ts = int(time.time()) - self.segment_age
        return f"#EXTM3U\n#EXT-X-TARGETDURATION:4\n#EXT-X-MEDIA-SEQUENCE:1\n#EXTINF:4,\nseg_{ts - 4}.ts\n#EXTINF:4,\nseg_{ts}.ts\n"


@pytest.fixture()
def world(monkeypatch, vultr_srv, tmp_path):
    state, base = vultr_srv
    w = World()
    env = {"VULTR_API_BASE": base, "VULTR_API_KEY": "vultr-key-for-tests-123", "VULTR_REGION": "bom", "VULTR_PLAN": "vc2-4c-8gb",
           "VULTR_SNAPSHOT_ID": "snap-1", "VULTR_RESERVED_IP_ID": "rip-1", "ORIGIN_HOST": "origin.test", "INGEST_HOST": "ingest.test",
           "LIVE_HOST": "live.test", "PUBLISH_USER": "streamer", "PUBLISH_PASS": PASS, "STREAMER_IPS": "any",
           "R2_BUCKET": "qualify-ias", "R2_ACCOUNT_ID": "acct", "R2_ACCESS_KEY_ID": "id-for-tests", "R2_SECRET_ACCESS_KEY": "secret-for-tests-123",
           "LMS_R2_ROOT": "cms"}
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(livestack, "STATE_DIR", tmp_path)
    monkeypatch.setattr(livestack, "STATE_FILE", tmp_path / "session.json")
    monkeypatch.setattr(livestack, "HISTORY_DIR", tmp_path / "history")
    monkeypatch.setattr(livestack, "BOOT_TIMES", tmp_path / "boot_times.csv")
    monkeypatch.setattr(livestack, "ssh", w.ssh)
    monkeypatch.setattr(livestack, "tcp_open", lambda *a, **k: True)
    monkeypatch.setattr(livestack, "https_ok", lambda url, timeout=10: (w.origin_ok, "HTTP 200" if w.origin_ok else "connection refused"))
    monkeypatch.setattr(livestack, "alert", lambda *a, **k: True)
    monkeypatch.setattr(cms_bridge, "alert", lambda *a, **k: True)
    monkeypatch.setattr(probe, "fetch_text", w.fetch_text)
    monkeypatch.setattr(replay, "s3_client", lambda state=False: World.S3(w))
    monkeypatch.setattr(cms_bridge.subprocess, "Popen", w.popen)
    w.state = state
    return w


def make_session(store, with_class=True, status="planned", **extra):
    cid = None
    if with_class:
        cid = store.q("insert into public.classes (title, status, scheduled_start) values ('Polity 1','scheduled', now()) returning id")[0]["id"]
    row = store.q("insert into public.live_sessions (title, class_id, status) values ('Polity live', %s, %s) returning id", (cid, status))[0]
    if extra:
        store.update(row["id"], **extra)
    return row["id"], cid


def events(store, sid):
    return [r["message"] for r in store.q("select message from public.live_events where session_id = %s order by id", (sid,))]


# =========================================================================== pure helpers
def test_parse_status():
    text = '###paths\n{"items":[{"name":"live/abc","ready":true},{"name":"live/other","ready":false}]}\n###speed\nspeed=0.93x\n###disk\n 42%\n'
    got = cms_bridge.parse_status(text, "live/abc")
    assert (got["ingest_live"], got["encoder_speed"], got["disk_percent"], got["ingest_bytes"]) == (True, 0.93, 42, None)
    assert cms_bridge.parse_status(text, "live/other")["ingest_live"] is False
    empty = cms_bridge.parse_status("", "live/abc")
    assert (empty["ingest_live"], empty["encoder_speed"], empty["disk_percent"]) == (False, None, None)
    with_bytes = '###paths\n{"items":[{"name":"live/abc","ready":true,"bytesReceived":123456}]}\n###log\nline one\nline two\n'
    got = cms_bridge.parse_status(with_bytes, "live/abc")
    assert got["ingest_bytes"] == 123456 and got["server_log"] == "line one\nline two"


def test_redact_hides_secrets(monkeypatch):
    monkeypatch.setenv("PUBLISH_PASS", PASS)
    out = cms_bridge.redact(f"failed: ssh with {PASS} and url x?user=a&pass=hunter22")
    assert PASS not in out and "hunter22" not in out


def test_missing_lists_unset(monkeypatch):
    for n in cms_bridge.START_REQUIRED:
        monkeypatch.delenv(n, raising=False)
    monkeypatch.setenv("VULTR_REGION", "bom")
    assert "VULTR_API_KEY" in cms_bridge.missing() and "VULTR_REGION" not in cms_bridge.missing()


# =========================================================================== database rules
def test_claim_is_atomic_and_single_server(store):
    a, _ = make_session(store)
    b, _ = make_session(store)
    row, why = store.claim(a, ("planned",), "starting")
    assert row and row["status"] == "starting"
    assert store.claim(a, ("planned",), "starting")[1] == "status"      # double click
    assert store.claim(b, ("planned",), "starting") == (None, "busy")   # second server refused by the database


# =========================================================================== authorisation + button handling
def test_only_admins_and_operators_may_act(store, world):
    sid, _ = make_session(store)
    assert service.act(store, sid, "start", None)[0] == 403
    assert service.act(store, sid, "start", str(uuid.uuid4()))[0] == 403
    assert service.act(store, sid, "start", EDITOR)[0] == 403
    assert store.get(sid)["status"] == "planned"


def test_start_button_claims_and_launches_job(store, world, monkeypatch):
    launched = []
    monkeypatch.setattr(service, "launch_job", lambda args: launched.append(args) or "exec-1")
    sid, _ = make_session(store)
    code, body = service.act(store, sid, "start", OPERATOR)
    assert code == 202 and body["ok"]
    assert launched == [["start", sid]]
    row = store.get(sid)
    assert row["status"] == "starting" and row["requested_by"] == OPERATOR and row["job_execution"] == "exec-1"
    assert service.act(store, sid, "start", ADMIN)[0] == 409              # already starting
    assert any("Refused start" in m for m in events(store, sid))


def test_start_refused_when_settings_missing(store, world, monkeypatch):
    monkeypatch.delenv("VULTR_API_KEY")
    sid, _ = make_session(store)
    code, body = service.act(store, sid, "start", ADMIN)
    assert code == 409 and "VULTR_API_KEY" in body["message"]
    assert store.get(sid)["status"] == "planned"
    assert any("VULTR_API_KEY" in m for m in events(store, sid))


def test_start_refused_while_another_session_runs(store, world, monkeypatch):
    monkeypatch.setattr(service, "launch_job", lambda args: "x")
    a, _ = make_session(store, status="live", instance_id="inst-9")
    b, _ = make_session(store)
    code, body = service.act(store, b, "start", ADMIN)
    assert code == 409 and "already running" in body["message"]


def test_job_launch_failure_reverts_status(store, world, monkeypatch):
    def boom(args):
        raise RuntimeError("quota exceeded")
    monkeypatch.setattr(service, "launch_job", boom)
    sid, _ = make_session(store)
    code, _ = service.act(store, sid, "start", ADMIN)
    assert code == 502 and store.get(sid)["status"] == "planned" and "quota exceeded" in store.get(sid)["error"]


def test_end_buttons(store, world, monkeypatch):
    launched = []
    monkeypatch.setattr(service, "launch_job", lambda args: launched.append(args) or "e")
    planned, _ = make_session(store)
    assert service.act(store, planned, "end", ADMIN)[0] == 409           # nothing running
    live, _ = make_session(store, with_class=False, status="live", instance_id="inst-1")
    assert service.act(store, live, "end", OPERATOR)[0] == 202
    assert launched[-1] == ["end", live, "save"] and store.get(live)["status"] == "ending"


def test_destroy_button_uses_discard_mode(store, world, monkeypatch):
    launched = []
    monkeypatch.setattr(service, "launch_job", lambda args: launched.append(args) or "e")
    sid, _ = make_session(store, with_class=False, status="ready", instance_id="inst-1")
    assert service.act(store, sid, "destroy", ADMIN)[0] == 202
    assert launched == [["end", sid, "discard"]]


def test_failed_end_cannot_be_restarted_as_start(store, world, monkeypatch):
    monkeypatch.setattr(service, "launch_job", lambda args: "x")
    sid, _ = make_session(store, with_class=False, status="failed", instance_id="inst-1", replay_status="failed")
    code, body = service.act(store, sid, "start", ADMIN)
    assert code == 409 and "End session" in body["message"]


# =========================================================================== start (real livestack.cmd_start)
def test_run_start_happy_path(store, world):
    sid, cid = make_session(store)
    store.claim(sid, ("planned",), "starting")
    assert cms_bridge.run_start(store, sid) == 0
    row = store.get(sid)
    assert row["status"] == "ready" and row["stage"] == "ready" and row["error"] is None
    assert row["instance_id"] == "inst-1" and row["main_ip"] == "203.0.113.7" and row["reserved_ip"] == "198.51.100.9"
    assert row["obs_server"] == "rtmp://ingest.test/live" and row["ready_at"] and row["boot_seconds"] is not None
    # the stream path belongs to the class, so students' playback matches the stream
    path = store.get_class(cid)["stream_path"]
    assert path == "live/" + row["stream_name"]
    key = store.q("select obs_stream_key from public.live_session_secrets where session_id = %s", (sid,))[0]["obs_stream_key"]
    assert key == f"{row['stream_name']}?user=streamer&pass={PASS}"
    log_text = " ".join(events(store, sid))
    assert PASS not in log_text and "READY" in log_text and "Server created" in log_text
    # the server was created with the snapshot, and the reserved IP is attached to it
    assert world.state["created"][0]["snapshot_id"] == "snap-1" and world.state["created"][0]["label"].startswith("livestack-")
    assert world.state["rip"]["instance_id"] == "inst-1"
    # no secret ever lands in a table an editor can read
    assert PASS not in json.dumps(store.q("select * from public.live_sessions"), default=str)


def test_run_start_without_class_uses_random_stream(store, world):
    sid, _ = make_session(store, with_class=False)
    store.claim(sid, ("planned",), "starting")
    assert cms_bridge.run_start(store, sid) == 0
    assert len(store.get(sid)["stream_name"]) >= 8


def test_run_start_reports_unhealthy_server(store, world):
    world.boot_rc = 1
    sid, _ = make_session(store)
    store.claim(sid, ("planned",), "starting")
    assert cms_bridge.run_start(store, sid) != 0
    row = store.get(sid)
    assert row["status"] == "failed" and "health check failed" in row["error"] and row["instance_id"] == "inst-1"
    assert world.state["deleted"] == []                                 # failure never destroys the server


def test_run_start_failure_is_recorded_without_secrets(store, world, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError(f"ssh rejected password {PASS}")
    monkeypatch.setattr(livestack, "tcp_open", boom)
    sid, _ = make_session(store)
    store.claim(sid, ("planned",), "starting")
    assert cms_bridge.run_start(store, sid) != 0
    row = store.get(sid)
    assert row["status"] == "failed" and PASS not in (row["error"] or "") and PASS not in " ".join(events(store, sid))


def test_run_start_missing_settings(store, world, monkeypatch):
    monkeypatch.delenv("PUBLISH_PASS")
    sid, _ = make_session(store)
    store.claim(sid, ("planned",), "starting")
    assert cms_bridge.run_start(store, sid) == 1
    assert store.get(sid)["status"] == "failed" and "PUBLISH_PASS" in store.get(sid)["error"]


def test_run_start_resumes_an_existing_server(store, world):
    sid, _ = make_session(store)
    store.claim(sid, ("planned",), "starting")
    world.boot_rc = 1
    cms_bridge.run_start(store, sid)
    world.boot_rc = 0
    store.claim(sid, ("failed",), "starting")
    assert cms_bridge.run_start(store, sid) == 0
    assert len(world.state["created"]) == 1                              # adopted by label, not created twice
    assert store.get(sid)["status"] == "ready"


# =========================================================================== monitoring
def ready_session(store, world, **extra):
    sid, cid = make_session(store, status="ready", stream_name="stream1", instance_id="inst-1", main_ip="203.0.113.7",
                            started_at=cms_bridge.now(), **extra)
    world.state["instances"]["inst-1"] = {"id": "inst-1", "label": "livestack-t", "status": "active", "power_status": "running", "main_ip": "203.0.113.7"}
    return sid, cid


def test_monitor_goes_live_and_back(store, world):
    sid, cid = ready_session(store, world)
    r = cms_bridge.monitor_session(store, store.get(sid))
    assert r["status"] == "ready" and store.get(sid)["ingest_live"] is False and store.get(sid)["health_ok"] is True
    world.ingest_live = True
    cms_bridge.monitor_session(store, store.get(sid))
    row = store.get(sid)
    assert row["status"] == "live" and row["live_since"] and row["cdn_ok"] is True and row["playlist_age_seconds"] <= 8
    assert store.get_class(cid)["status"] == "live" and store.get_class(cid)["actual_start"]
    assert "Stream started" in " ".join(events(store, sid))
    world.ingest_live = False
    cms_bridge.monitor_session(store, store.get(sid))
    assert store.get(sid)["status"] == "ready"
    assert any("Stream stopped" in m for m in events(store, sid))


def test_monitor_records_bitrate_log_peak_and_history(store, world):
    sid, cid = ready_session(store, world)
    world.ingest_live, world.bytes_received, world.log = True, 1_000_000, f"mediamtx: publisher connected pass={PASS}"
    store.q("insert into public.users (id, name) values ('u1','A'), ('u2','B')")
    store.q("insert into public.class_attendance (class_id, user_id, mode) values (%s,'u1','live'), (%s,'u2','live')", (cid, cid))
    cms_bridge.monitor_session(store, store.get(sid))
    assert store.get(sid)["ingest_kbps"] is None or store.get(sid)["ingest_kbps"] >= 0   # first pass has no previous counter
    # a minute later OBS has sent 33.75 MB more: 4500 kbps
    store.q("update public.live_sessions set last_checked_at = now() - interval '60 seconds' where id = %s", (sid,))
    world.bytes_received = 1_000_000 + 33_750_000
    cms_bridge.monitor_session(store, store.get(sid))
    row = store.get(sid)
    assert 4400 <= row["ingest_kbps"] <= 4600
    assert row["peak_students"] == 2 and row["students_watching"] == 2
    assert "publisher connected" in row["server_log"] and PASS not in row["server_log"]
    hist = store.q("select * from public.live_metrics where session_id = %s order by id", (sid,))
    assert len(hist) == 2 and hist[-1]["students_watching"] == 2 and hist[-1]["ingest_live"] is True and hist[-1]["health_ok"] is True
    store.q("delete from public.class_attendance where user_id = 'u2'")
    cms_bridge.monitor_session(store, store.get(sid))
    assert store.get(sid)["students_watching"] == 1 and store.get(sid)["peak_students"] == 2     # peak never drops


def test_ingest_kbps_is_zero_when_not_publishing(store, world):
    sid, _ = ready_session(store, world)
    cms_bridge.monitor_session(store, store.get(sid))
    assert store.get(sid)["ingest_kbps"] == 0


def test_monitor_counts_students_watching(store, world):
    sid, cid = ready_session(store, world)
    store.q("insert into public.users (id, name) values ('u1','A'), ('u2','B'), ('u3','C')")
    store.q("insert into public.class_attendance (class_id, user_id, mode, last_seen_at) values (%s,'u1','live',now()), (%s,'u2','live',now() - interval '10 minutes'), (%s,'u3','replay',now())", (cid, cid, cid))
    cms_bridge.monitor_session(store, store.get(sid))
    assert store.get(sid)["students_watching"] == 1


def test_monitor_flags_slow_encoder_and_stale_cdn(store, world):
    sid, _ = ready_session(store, world)
    world.ingest_live, world.encoder_speed = True, 0.80
    cms_bridge.monitor_session(store, store.get(sid))
    row = store.get(sid)
    assert row["health_ok"] is False and "encoder 0.80x" in row["health_detail"]
    world.encoder_speed, world.segment_age = 1.0, 60
    cms_bridge.monitor_session(store, store.get(sid))
    row = store.get(sid)
    assert row["health_ok"] is False and row["cdn_ok"] is False


def test_monitor_flags_origin_down_and_ssh_failure(store, world):
    sid, _ = ready_session(store, world)
    world.origin_ok = False
    cms_bridge.monitor_session(store, store.get(sid))
    assert store.get(sid)["health_ok"] is False and "origin DOWN" in store.get(sid)["health_detail"]
    world.origin_ok, world.ssh_ok = True, False
    cms_bridge.monitor_session(store, store.get(sid))
    assert store.get(sid)["health_ok"] is False and "SSH" in store.get(sid)["health_detail"]


def test_monitor_detects_server_deleted_outside_cms(store, world):
    sid, _ = ready_session(store, world)
    world.state["instances"].clear()
    cms_bridge.monitor_session(store, store.get(sid))
    row = store.get(sid)
    assert row["status"] == "failed" and "no longer exists" in row["error"]


def test_monitor_all_alerts_on_orphan_servers(store, world, monkeypatch):
    sent = []
    monkeypatch.setattr(cms_bridge, "alert", lambda msg, level="warning": sent.append(msg) or True)
    world.state["instances"]["stray-1"] = {"id": "stray-1", "label": "livestack-forgotten", "status": "active", "power_status": "running"}
    out = cms_bridge.monitor_all(store)
    assert {"orphans": ["livestack-forgotten"]} in out and any("forgotten" in m for m in sent)


# =========================================================================== end
def live_session(store, world, **extra):
    sid, cid = ready_session(store, world, **extra)
    store.update(sid, status="live", live_since=cms_bridge.now())
    store.claim(sid, ("live",), "ending")
    world.stream = "stream1"
    return sid, cid


def test_end_saves_verified_replay_then_destroys(store, world):
    sid, cid = live_session(store, world)
    rc = cms_bridge.run_end(store, sid, "save")
    assert rc == 0, store.get(sid)["error"]
    row = store.get(sid)
    assert row["status"] == "ended" and row["ended_at"] and row["replay_status"] == "verified"
    assert row["replay_sha256"] == RECORDING_SHA and row["replay_size_bytes"] == len(RECORDING)
    # replay is stored under cms/ in the new bucket, and the DB key is relative to that root
    [(key, obj)] = world.uploaded.items()
    assert key.startswith("cms/replays/") and key.endswith(".mp4") and obj["data"] == RECORDING
    assert row["replay_key"] == key[len("cms/"):]
    asset = store.q("select * from public.video_assets where id = %s", (row["video_asset_id"],))[0]
    assert asset["status"] == "ready" and asset["source_key"] == row["replay_key"] and asset["class_id"] == cid
    assert asset["duration_seconds"] == 3601 and asset["provider"] == "r2" and asset["sha256"] == RECORDING_SHA
    cls = store.get_class(cid)
    assert cls["status"] == "published"
    assert world.state["deleted"] == ["inst-1"] and world.state["rip"]["subnet"]     # server gone, reserved IP kept


def test_end_does_not_destroy_when_no_recording(store, world):
    world.recording = False
    sid, cid = live_session(store, world)
    assert cms_bridge.run_end(store, sid, "save") == 3
    row = store.get(sid)
    assert row["status"] == "failed" and row["replay_status"] == "failed" and "NOT destroyed" in row["error"]
    assert world.state["deleted"] == [] and "inst-1" in world.state["instances"]
    assert store.get_class(cid)["status"] != "published"


def test_end_does_not_destroy_when_upload_is_corrupted(store, world):
    world.truncate_upload = True
    sid, _ = live_session(store, world)
    assert cms_bridge.run_end(store, sid, "save") == 3
    assert store.get(sid)["status"] == "failed" and world.state["deleted"] == []
    assert world.uploaded == {} and len(world.deleted_keys) == 1            # the partial object was removed


def test_end_retry_after_failure_succeeds(store, world):
    world.recording = False
    sid, _ = live_session(store, world)
    cms_bridge.run_end(store, sid, "save")
    world.recording = True
    assert store.claim(sid, ("failed",), "ending")[0]
    assert cms_bridge.run_end(store, sid, "save") == 0
    assert store.get(sid)["status"] == "ended" and world.state["deleted"] == ["inst-1"]


def test_end_skips_upload_when_replay_already_in_r2(store, world):
    sid, _ = live_session(store, world)
    cms_bridge.run_end(store, sid, "save")
    key = next(iter(world.uploaded))
    world.state["instances"]["inst-1"] = {"id": "inst-1", "label": "livestack-t", "status": "active", "power_status": "running", "main_ip": "203.0.113.7"}
    store.update(sid, status="ending")
    world.uploaded[key]["data"] = RECORDING
    count = len(world.uploaded)
    world.truncate_upload = True      # would corrupt a fresh upload; must not be attempted
    assert cms_bridge.run_end(store, sid, "save") == 0
    assert len(world.uploaded) == count and world.uploaded[key]["data"] == RECORDING


def test_destroy_discard_mode(store, world):
    sid, cid = live_session(store, world)
    assert cms_bridge.run_end(store, sid, "discard") == 0
    row = store.get(sid)
    assert row["status"] == "ended" and row["replay_status"] == "skipped" and world.uploaded == {}
    assert world.state["deleted"] == ["inst-1"]
    assert store.get_class(cid)["status"] == "processing"                    # staff add a recording later
    assert any("WITHOUT" in m for m in events(store, sid))


def test_end_when_server_already_gone(store, world):
    sid, _ = live_session(store, world)
    world.state["instances"].clear()
    assert cms_bridge.run_end(store, sid, "save") == 0
    assert store.get(sid)["status"] == "ended"


def test_never_destroys_foreign_servers(store, world):
    world.state["instances"]["inst-1"] = {"id": "inst-1", "label": "production-web", "status": "active", "power_status": "running", "main_ip": "203.0.113.7"}
    sid, _ = make_session(store, with_class=False, status="ending", instance_id="inst-1", main_ip="203.0.113.7", stream_name="stream1")
    with pytest.raises(SystemExit):
        cms_bridge.run_end(store, sid, "discard")
    assert world.state["deleted"] == [] and store.get(sid)["status"] == "failed"


# =========================================================================== stuck jobs
def test_reconcile_stuck_jobs(store, world, monkeypatch):
    sid, _ = make_session(store, with_class=False, status="starting")
    store.q("alter table public.live_sessions disable trigger trg_touch")      # the trigger would reset date_updated
    store.q("update public.live_sessions set date_updated = now() - interval '80 minutes' where id = %s", (sid,))
    store.q("alter table public.live_sessions enable trigger trg_touch")
    assert service.reconcile_stuck(store) == [store.get(sid)["session_code"]]
    assert store.get(sid)["status"] == "failed" and "ended without finishing" in store.get(sid)["error"]
    fresh, _ = make_session(store, with_class=False, status="ending")
    monkeypatch.setattr(service, "execution_finished", lambda name: True)
    assert service.reconcile_stuck(store) == []                              # too fresh: the job may still be writing its result


# =========================================================================== HTTP service
@pytest.fixture()
def http(store, world, monkeypatch):
    monkeypatch.setenv("LIVESTREAM_CONTROL_TOKEN", "t" * 40)
    launched = []
    monkeypatch.setattr(service, "launch_job", lambda args: launched.append(args) or "exec-http")
    service.Handler.store = store
    srv = ThreadingHTTPServer(("127.0.0.1", 0), service.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"

    def call(path, body=None, token="t" * 40, method="POST"):
        req = urllib.request.Request(base + path, method=method, data=json.dumps(body or {}).encode() if method == "POST" else None,
                                     headers={"Content-Type": "application/json", **({"Authorization": f"Bearer {token}"} if token else {})})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())
    yield call, launched
    srv.shutdown()


def test_http_requires_the_shared_token(http, store):
    call, launched = http
    sid, _ = make_session(store)
    assert call(f"/v1/sessions/{sid}/start", {"user": ADMIN}, token=None)[0] == 401
    assert call(f"/v1/sessions/{sid}/start", {"user": ADMIN}, token="wrong" * 8)[0] == 401
    assert call("/v1/tick", token=None)[0] == 401
    assert call("/healthz", method="GET", token=None)[0] == 200
    assert launched == [] and store.get(sid)["status"] == "planned"


def test_http_start_and_validation(http, store):
    call, launched = http
    sid, _ = make_session(store)
    code, body = call(f"/v1/sessions/{sid}/start", {"user": OPERATOR})
    assert code == 202 and body["ok"] and launched == [["start", sid]]
    assert call(f"/v1/sessions/{sid}/start", {"user": EDITOR})[0] == 403
    assert call("/v1/sessions/not-a-uuid/start", {"user": ADMIN})[0] == 404
    assert call(f"/v1/sessions/{uuid.uuid4()}/start", {"user": ADMIN})[0] == 404
    assert call(f"/v1/sessions/{sid}/explode", {"user": ADMIN})[0] == 404


def test_http_flow_callers_get_refusals_as_ok_false(http, store):
    call, launched = http
    sid, _ = make_session(store)
    code, body = call(f"/v1/sessions/{sid}/end", {"user": ADMIN, "flow": True})      # nothing running: refused
    assert code == 200 and body["ok"] is False and "no running server" in body["message"]
    code, body = call(f"/v1/sessions/{sid}/start", {"user": EDITOR, "flow": True})   # not an operator
    assert code == 200 and body["ok"] is False
    assert call(f"/v1/sessions/{sid}/end", {"user": ADMIN})[0] == 409                # other callers still get real status codes
    assert call(f"/v1/sessions/{sid}/end", {"user": ADMIN, "flow": True}, token=None)[0] == 401   # auth is never softened
    assert launched == []


def test_http_tick_monitors_running_sessions(http, store, world):
    call, _ = http
    sid, _ = ready_session(store, world)
    code, body = call("/v1/tick")
    assert code == 200 and body["ok"] and body["monitored"][0]["status"] == "ready"
    assert store.get(sid)["last_checked_at"] is not None


def test_tick_keeps_cms_warm_only_while_a_session_runs(store, world, monkeypatch):
    pings = []
    monkeypatch.setattr(service, "keep_cms_warm", lambda: pings.append(1) or True)
    assert service.tick(store)["active"] is False and pings == []
    ready_session(store, world)
    out = service.tick(store)
    assert out["active"] is True and out["cms_warmed"] is True and pings == [1]


def test_keep_warm_needs_a_url_and_never_raises(monkeypatch):
    monkeypatch.delenv("ADMIN_PING_URL", raising=False)
    assert service.keep_cms_warm() is False
    monkeypatch.setenv("ADMIN_PING_URL", "http://127.0.0.1:9/never")
    assert service.keep_cms_warm() is False


def test_store_refuses_tls_less_remote_database():
    s = cms_bridge.Store("postgres://u:p@db.example.com:5432/postgres")
    with pytest.raises(RuntimeError, match="CA certificate"):
        s._connect()
    with pytest.raises(RuntimeError, match="6543"):
        cms_bridge.Store("postgres://u:p@db.example.com:6543/postgres", "pem")._connect()
