"""replay.py against a local S3-compatible server (moto), the same API R2 speaks.

No Cloudflare account or credentials needed. Exercises the real code paths:
upload (single + multipart), sha256 metadata, skip-if-identical, verify,
presigned GET over plain HTTP, selftest and CORS.

Skipped automatically if moto is not installed:  pip install -r requirements-dev.txt
"""
import logging
import socket
import urllib.request

import pytest

moto_server = pytest.importorskip("moto.server")
import replay  # noqa: E402

MB = 1024 * 1024


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def endpoint():
    port = _free_port()
    srv = moto_server.ThreadedMotoServer(ip_address="127.0.0.1", port=port, verbose=False)
    srv.start()
    yield f"http://127.0.0.1:{port}"
    srv.stop()


@pytest.fixture
def r2(endpoint, monkeypatch):
    for k, v in {"R2_ENDPOINT_URL": endpoint, "R2_ACCESS_KEY_ID": "test", "R2_SECRET_ACCESS_KEY": "test",
                 "R2_BUCKET": "lms-replays", "STATE_BUCKET": "", "R2_ACCOUNT_ID": ""}.items():
        monkeypatch.setenv(k, v)
    # Buckets are created in the R2 dashboard in real life. moto refuses CreateBucket
    # with R2's region "auto", so create the test bucket with a plain us-east-1 client.
    import boto3
    admin = boto3.client("s3", endpoint_url=endpoint, region_name="us-east-1",
                         aws_access_key_id="test", aws_secret_access_key="test")
    try:
        admin.create_bucket(Bucket="lms-replays")
    except admin.exceptions.BucketAlreadyOwnedByYou:
        pass
    return replay.s3_client()                     # the exact client replay.py uses for R2


def test_endpoint_defaults_to_account_url(monkeypatch):
    monkeypatch.delenv("R2_ENDPOINT_URL", raising=False)
    assert replay.endpoint_url("abc123") == "https://abc123.r2.cloudflarestorage.com"
    monkeypatch.setenv("R2_ENDPOINT_URL", "https://abc123.eu.r2.cloudflarestorage.com/")
    assert replay.endpoint_url("abc123") == "https://abc123.eu.r2.cloudflarestorage.com"


def test_upload_verify_and_skip_when_identical(r2, tmp_path, caplog):
    f = tmp_path / "small.mp4"
    f.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"x" * 5000)
    m = replay.upload(f, "replays/2026/11/a.mp4", s3=r2)
    assert m["size"] == f.stat().st_size and len(m["sha256"]) == 64

    h = r2.head_object(Bucket="lms-replays", Key="replays/2026/11/a.mp4")
    assert h["ContentType"] == "video/mp4" and h["Metadata"]["sha256"] == m["sha256"]

    caplog.set_level(logging.INFO, logger="livestack")
    replay.upload(f, "replays/2026/11/a.mp4", s3=r2)            # second run must not re-upload
    assert "already uploaded" in caplog.text

    ok, msg = replay.verify_object("replays/2026/11/a.mp4", f.stat().st_size, m["sha256"], s3=r2)
    assert ok, msg


def test_verify_detects_wrong_size_sha_and_missing(r2, tmp_path):
    f = tmp_path / "b.mp4"
    f.write_bytes(b"y" * 1000)
    m = replay.upload(f, "t/b.mp4", s3=r2)
    assert not replay.verify_object("t/b.mp4", 999, s3=r2)[0]
    assert not replay.verify_object("t/b.mp4", 1000, "0" * 64, s3=r2)[0]
    assert replay.verify_object("t/b.mp4", 1000, m["sha256"], s3=r2)[0]
    assert not replay.verify_object("t/missing.mp4", 1000, s3=r2)[0]


def test_changed_file_is_uploaded_again(r2, tmp_path, caplog):
    f = tmp_path / "c.mp4"
    f.write_bytes(b"1" * 2000)
    first = replay.upload(f, "t/c.mp4", s3=r2)
    f.write_bytes(b"2" * 2000)                                    # same size, different content
    caplog.set_level(logging.INFO, logger="livestack")
    second = replay.upload(f, "t/c.mp4", s3=r2)
    assert second["sha256"] != first["sha256"] and "uploading" in caplog.text
    assert r2.head_object(Bucket="lms-replays", Key="t/c.mp4")["Metadata"]["sha256"] == second["sha256"]


def test_multipart_upload_large_file(r2, tmp_path):
    f = tmp_path / "big.mp4"
    with f.open("wb") as fh:                                       # 70 MB -> two 64 MB-sized parts
        fh.write(b"z" * (70 * MB))
    m = replay.upload(f, "t/big.mp4", s3=r2)
    h = r2.head_object(Bucket="lms-replays", Key="t/big.mp4")
    assert h["ContentLength"] == 70 * MB
    assert "-" in h["ETag"], "expected a multipart ETag (<md5>-<parts>)"
    assert h["Metadata"]["sha256"] == m["sha256"]


def test_presigned_url_downloads_over_plain_http(r2, tmp_path):
    f = tmp_path / "p.mp4"
    f.write_bytes(b"presigned-content")
    replay.upload(f, "t/p.mp4", s3=r2)
    url = replay.presign("t/p.mp4", 300, s3=r2)
    assert "X-Amz-Signature" in url and "X-Amz-Expires=300" in url
    with urllib.request.urlopen(url, timeout=10) as r:
        assert r.read() == b"presigned-content"


def test_selftest_passes_and_cleans_up(r2, capsys):
    assert replay.selftest(s3=r2) == 0
    out = capsys.readouterr().out
    assert "all checks passed" in out and "presigned URL" in out
    left = r2.list_objects_v2(Bucket="lms-replays", Prefix="selftest/").get("KeyCount", 0)
    assert left == 0


def test_selftest_reports_missing_bucket(r2, monkeypatch, capsys):
    monkeypatch.setenv("R2_BUCKET", "does-not-exist")
    assert replay.selftest(s3=r2) == 1
    assert "does not exist" in capsys.readouterr().out


def test_cors_rules_file_is_valid_for_s3_api(r2):
    import json
    rules = json.loads((replay.ROOT / "config" / "r2-cors.json").read_text(encoding="utf-8"))
    r2.put_bucket_cors(Bucket="lms-replays", CORSConfiguration={"CORSRules": rules})
    got = r2.get_bucket_cors(Bucket="lms-replays")["CORSRules"]
    assert got[0]["AllowedMethods"] == rules[0]["AllowedMethods"]
