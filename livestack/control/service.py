#!/usr/bin/env python3
"""Livestream control service: the endpoint behind the CMS buttons (Cloud Run, scales to zero).

Directus Flows call it with a shared secret; it never does long work itself:
  * start / end / destroy  -> claim the session in the database, then launch a Cloud Run Job
  * refresh                -> one monitoring pass (a few seconds)
  * tick                   -> monitoring pass for every running session + forgotten-server check
                              (Cloud Scheduler calls this every few minutes)

  POST /v1/sessions/<id>/start | end | destroy | refresh      body: {"user": "<directus user id>"}
  POST /v1/tick
  GET  /health   (also /healthz; Cloud Run itself reserves /healthz, so use /health there)

Auth: `Authorization: Bearer $LIVESTREAM_CONTROL_TOKEN`, plus, for session actions, the Directus
user must be an Administrator or hold the "Stream Operator" policy (checked in the database).
"""
from __future__ import annotations

import hmac
import json
import os
import re
import subprocess
import sys
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import cms_bridge  # noqa: E402
from cms_bridge import Store  # noqa: E402
from common import log, setup_logging  # noqa: E402

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
ROUTE_RE = re.compile(r"^/v1/sessions/([0-9a-f-]{36})/(start|end|destroy|refresh)$")


# =========================================================================== Cloud Run Jobs
def _metadata_token() -> str:
    req = urllib.request.Request("http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token",
                                 headers={"Metadata-Flavor": "Google"})
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read())["access_token"]


def _run_api(method: str, url: str, body: dict | None = None) -> dict:
    req = urllib.request.Request(url, method=method, data=None if body is None else json.dumps(body).encode(),
                                 headers={"Authorization": f"Bearer {_metadata_token()}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = r.read()
        return json.loads(raw) if raw.strip() else {}


def launch_job(args: list[str]) -> str:
    """Start the long-running job. Returns an execution id for the timeline."""
    if os.environ.get("LOCAL_JOB_RUNNER") == "1":  # development/tests: run in a local process
        p = subprocess.Popen([sys.executable, str(HERE / "job.py"), *args])
        return f"local-{p.pid}"
    project = os.environ["GOOGLE_CLOUD_PROJECT"]
    region = os.environ.get("CLOUD_RUN_REGION", "asia-south1")
    job = os.environ.get("LIVE_JOB_NAME", "qualify-livestream-job")
    resp = _run_api("POST", f"https://run.googleapis.com/v2/projects/{project}/locations/{region}/jobs/{job}:run",
                    {"overrides": {"containerOverrides": [{"args": ["control/job.py", *args]}]}})
    return (resp.get("metadata") or {}).get("name") or resp.get("name") or "started"


def execution_finished(name: str) -> bool:
    """True when a launched job execution has completed (successfully or not)."""
    if not name or name.startswith("local-") or "/executions/" not in name:
        return False
    try:
        ex = _run_api("GET", f"https://run.googleapis.com/v2/{name}")
    except Exception as e:  # noqa: BLE001
        log.warning("could not read execution %s: %s", name, e)
        return False
    return bool(ex.get("completionTime"))


# =========================================================================== authorisation
def operator_allowed(store: Store, user: str | None) -> bool:
    """Administrators and members of the 'Stream Operator' policy may run live sessions."""
    if not user or not UUID_RE.match(user):
        return False
    rows = store.q(
        'select 1 from public.directus_users u '
        'join public.directus_access a on a.role = u.role or a."user" = u.id '
        'join public.directus_policies p on p.id = a.policy '
        "where u.id = %s and u.status = 'active' and (p.admin_access or p.name = 'Stream Operator') limit 1", (user,))
    return bool(rows)


# =========================================================================== actions
def act(store: Store, sid: str, action: str, user: str | None) -> tuple[int, dict]:
    """Perform one button press. Returns (http status, json body). Every refusal is also written
    to the session timeline so the operator sees why in the CMS."""
    if not operator_allowed(store, user):
        return 403, {"ok": False, "message": "Only Administrators and Stream Operators can run live sessions."}
    row = store.get(sid)
    if not row:
        return 404, {"ok": False, "message": "Session not found."}

    def refuse(message: str, code: int = 409) -> tuple[int, dict]:
        store.event(sid, "warning", "control", f"Refused {action}: {message}")
        return code, {"ok": False, "message": message}

    if action == "refresh":
        if row["status"] in ("ready", "live") or (row["status"] == "failed" and row.get("instance_id")):
            return 200, {"ok": True, **cms_bridge.monitor_session(store, row)}
        return refuse(f"Nothing is running to monitor (status: {row['status']}).")

    if action == "start":
        problems = cms_bridge.missing()
        if problems:
            return refuse("Livestream settings are missing: " + ", ".join(problems) + ". Fill livestack/.env and run deploy-livestream.ps1.")
        if row["status"] == "failed" and row.get("replay_status") in ("uploading", "failed"):
            return refuse("This session failed while ending. Press 'End session' to retry saving the replay.")
        other = store.running_elsewhere(sid)
        if other:
            return refuse(f"Another session is already running ({other['title']}, {other['status']}). Only one server can run at a time.")
        claimed, why = store.claim(sid, ("planned", "failed"), "starting", requested_by=user, error=None, stage=None)
        if not claimed:
            return refuse("Start only works on planned or failed sessions." if why == "status" else "Another session is already running.")
        job_args = ["start", sid]
    else:  # end | destroy
        if row["status"] not in ("ready", "live", "failed") or (row["status"] == "failed" and not row.get("instance_id")):
            return refuse(f"There is no running server to end (status: {row['status']}).")
        claimed, why = store.claim(sid, ("ready", "live", "failed"), "ending", requested_by=user, error=None)
        if not claimed:
            return refuse("The session changed state; refresh the page and try again.")
        job_args = ["end", sid, "save" if action == "end" else "discard"]

    try:
        execution = launch_job(job_args)
    except Exception as e:  # noqa: BLE001
        msg = f"Could not launch the background job: {e}"
        store.update(sid, status=row["status"], error=cms_bridge.redact(msg)[:1000])
        store.event(sid, "error", "control", msg)
        return 502, {"ok": False, "message": msg}
    store.update(sid, job_execution=str(execution)[:300])
    verb = {"start": "Start requested", "end": "End requested (replay will be saved first)", "destroy": "Destroy requested (replay will be discarded)"}[action]
    store.event(sid, "info", "control", f"{verb}.")
    return 202, {"ok": True, "message": verb + ". Watch the timeline on this session.", "execution": str(execution)}


def reconcile_stuck(store: Store) -> list[str]:
    """Sessions stuck in starting/ending because their job died (crash, OOM, timeout)."""
    fixed = []
    for r in store.q("select id, session_code, status, job_execution, coalesce(date_updated, date_created) as touched "
                     "from public.live_sessions where status in ('starting','ending')"):
        age = (cms_bridge.now() - r["touched"]).total_seconds()
        dead = execution_finished(r.get("job_execution") or "") or age > 70 * 60
        if dead and age > 90:   # give a just-finished job a moment to write its final status
            msg = ("The background job ended without finishing (see Cloud Run logs). The server may still be running; "
                   "use 'Refresh monitoring', then End session or Destroy server.")
            store.update(r["id"], status="failed", error=msg)
            store.event(r["id"], "error", "control", msg)
            fixed.append(r["session_code"])
    return fixed


def keep_cms_warm() -> bool:
    """While a session is running, ping the admin dashboard so the operators' control room never hits a cold start."""
    url = os.environ.get("ADMIN_PING_URL")
    if not url:
        return False
    try:
        urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "livestream-control"}), timeout=25).close()
        return True
    except Exception as e:  # noqa: BLE001
        log.warning("CMS keep-warm ping failed: %s", e)
        return False


def tick(store: Store) -> dict:
    """Called every minute by Cloud Scheduler. The steady request flow is also what keeps this
    service (min instances 0) from scaling to zero while a stream is running."""
    stuck = reconcile_stuck(store)
    monitored = cms_bridge.monitor_all(store)
    active = bool(store.q("select 1 from public.live_sessions where status in ('starting','ready','live','ending') limit 1"))
    warmed = keep_cms_warm() if active else False
    try:
        store.purge_old_metrics()
    except Exception as e:  # noqa: BLE001
        log.warning("metric purge failed: %s", e)
    return {"ok": True, "active": active, "cms_warmed": warmed, "stuck_fixed": stuck, "monitored": monitored}


# =========================================================================== HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = "livestream-control"
    store: Store | None = None

    def log_message(self, fmt, *args):  # keep request lines, drop nothing sensitive (no bodies logged)
        log.info("%s %s", self.address_string(), fmt % args)

    def _send(self, code: int, body: dict) -> None:
        data = json.dumps(body, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _authorised(self) -> bool:
        token = os.environ.get("LIVESTREAM_CONTROL_TOKEN", "")
        given = self.headers.get("Authorization", "")
        return len(token) >= 32 and hmac.compare_digest(given.encode(), f"Bearer {token}".encode())

    def do_GET(self):  # noqa: N802
        if self.path in ("/health", "/healthz"):
            return self._send(200, {"ok": True})
        self._send(404, {"ok": False, "message": "not found"})

    def do_POST(self):  # noqa: N802
        if not self._authorised():
            return self._send(401, {"ok": False, "message": "unauthorised"})
        try:
            length = min(int(self.headers.get("Content-Length") or 0), 10_000)
            body = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, TypeError):
            return self._send(400, {"ok": False, "message": "invalid JSON"})
        try:
            if self.path == "/v1/tick":
                return self._send(200, tick(self.store))
            m = ROUTE_RE.match(self.path)
            if not m or not UUID_RE.match(m.group(1)):
                return self._send(404, {"ok": False, "message": "not found"})
            code, out = act(self.store, m.group(1), m.group(2), body.get("user"))
            # Directus Flows cannot read the body of an HTTP error, so for them a business refusal
            # (not allowed / not found / wrong state) is a normal answer with ok=false.
            if body.get("flow") and code in (403, 404, 409):
                code = 200
            return self._send(code, out)
        except Exception as e:  # noqa: BLE001
            log.exception("request failed: %s", e)
            return self._send(500, {"ok": False, "message": cms_bridge.redact(f"{type(e).__name__}: {e}")[:300]})


def main() -> int:
    setup_logging()
    cms_bridge.prepare_environment()
    if len(os.environ.get("LIVESTREAM_CONTROL_TOKEN", "")) < 32:
        log.error("LIVESTREAM_CONTROL_TOKEN must be set (32+ characters); refusing to start")
        return 1
    Handler.store = Store.from_env()
    port = int(os.environ.get("PORT", "8080"))
    log.info("livestream control listening on :%d", port)
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
