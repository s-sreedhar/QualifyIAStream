import datetime as dt
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

import livestack
from common import TransientError, parse_env_text
from livestack import Vultr, VultrError, server_env_text
from replay import default_key, safe_session


# --------------------------------------------------------------------------- env
def test_parse_env_text():
    env = parse_env_text("""
# comment
A=1
export B="two words"
C='x=y'
EMPTY=
""")
    assert env == {"A": "1", "B": "two words", "C": "x=y", "EMPTY": ""}


def test_server_env_never_contains_control_secrets(monkeypatch):
    monkeypatch.setenv("VULTR_API_KEY", "secret-vultr")
    monkeypatch.setenv("R2_SECRET_ACCESS_KEY", "secret-r2")
    monkeypatch.setenv("ORIGIN_HOST", "origin.example.com")
    monkeypatch.setenv("PUBLISH_PASS", "abcdefghijklmnop")
    text = server_env_text()
    assert "secret-vultr" not in text and "secret-r2" not in text
    assert "ORIGIN_HOST='origin.example.com'" in text


def test_ssh_sends_env_as_bytes_without_crlf(monkeypatch):
    captured = {}

    def fake_run(cmd, input=None, timeout=None, capture_output=False):
        captured["input"] = input
        class R: returncode, stdout, stderr = 0, b"", b""
        return R()

    monkeypatch.setattr(livestack.subprocess, "run", fake_run)
    livestack.ssh("203.0.113.5", "cat > /etc/livestack/env", input_text="A='1'\nB='2'\n")
    assert captured["input"] == b"A='1'\nB='2'\n"


def test_server_env_rejects_quotes(monkeypatch):
    monkeypatch.setenv("PUBLISH_PASS", "it's")
    with pytest.raises(SystemExit):
        server_env_text()


# --------------------------------------------------------------------------- replay keys
def test_default_key():
    when = dt.datetime(2026, 11, 11, tzinfo=dt.timezone.utc)
    assert default_key("20261111-1900", when) == "replays/2026/11/20261111-1900.mp4"
    assert safe_session("../../etc passwd") == "etc-passwd"
    assert safe_session("///") == "session"


# --------------------------------------------------------------------------- Vultr client
class FakeVultr(BaseHTTPRequestHandler):
    script: list = []          # queue of (status, body) responses
    seen: list = []

    def _reply(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        FakeVultr.seen.append((self.command, self.path, self.headers.get("Authorization"), body))
        status, payload = FakeVultr.script.pop(0)
        data = json.dumps(payload).encode() if payload is not None else b""
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    do_GET = do_POST = do_DELETE = _reply

    def log_message(self, *args):
        pass


@pytest.fixture
def fake():
    FakeVultr.script, FakeVultr.seen = [], []
    srv = HTTPServer(("127.0.0.1", 0), FakeVultr)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield Vultr("test-key", base=f"http://127.0.0.1:{srv.server_port}/v2", retry_delay=0.01)
    srv.shutdown()


def test_get_retries_on_429_and_sends_bearer(fake):
    FakeVultr.script = [(429, {"error": "slow down"}), (503, None), (200, {"instance": {"id": "i1"}})]
    assert fake.get_instance("i1") == {"id": "i1"}
    assert len(FakeVultr.seen) == 3
    assert FakeVultr.seen[0][2] == "Bearer test-key"


def test_get_instance_404_is_none(fake):
    FakeVultr.script = [(404, {"error": "not found"})]
    assert fake.get_instance("gone") is None


def test_client_error_not_retried(fake):
    FakeVultr.script = [(400, {"error": "bad plan"})]
    with pytest.raises(VultrError) as e:
        fake.list_instances()
    assert e.value.status == 400 and len(FakeVultr.seen) == 1


def test_create_is_not_blindly_retried(fake):
    FakeVultr.script = [(500, {"error": "oops"})]
    with pytest.raises(TransientError):
        fake.create_instance({"label": "livestack-x"})
    assert len(FakeVultr.seen) == 1                   # exactly one POST


def test_delete_404_is_ok(fake):
    FakeVultr.script = [(404, {"error": "not found"})]
    fake.delete_instance("gone")


def test_list_instances_follows_cursor(fake):
    FakeVultr.script = [
        (200, {"instances": [{"id": "a"}], "meta": {"links": {"next": "c2"}}}),
        (200, {"instances": [{"id": "b"}], "meta": {"links": {"next": ""}}}),
    ]
    assert [i["id"] for i in fake.list_instances(label="livestack-1")] == ["a", "b"]
    assert "cursor=c2" in FakeVultr.seen[1][1] and "label=livestack-1" in FakeVultr.seen[0][1]


def test_destroy_refuses_foreign_instances(fake):
    with pytest.raises(SystemExit):
        livestack.destroy_instance(fake, {"id": "x", "label": "production-db"})
    assert FakeVultr.seen == []


def test_state_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(livestack, "STATE_DIR", tmp_path)
    monkeypatch.setattr(livestack, "STATE_FILE", tmp_path / "session.json")
    monkeypatch.setattr(livestack, "HISTORY_DIR", tmp_path / "history")
    livestack.save_state({"session_id": "s1", "instance_id": "i1"})
    assert livestack.load_state()["instance_id"] == "i1"
    livestack.archive_state(livestack.load_state())
    assert livestack.load_state() == {}
    assert len(list((tmp_path / "history").iterdir())) == 1
