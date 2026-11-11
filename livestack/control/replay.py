#!/usr/bin/env python3
"""Replay pipeline: one MP4 per session in Cloudflare R2.

  run      INPUT --session-id ID   encode -> upload -> verify -> write manifest (the usual command)
  encode   INPUT OUTPUT            session.mkv -> replay.mp4 (H.264 CRF 23, AAC 128k, faststart)
  upload   FILE --key KEY          multipart upload with retries; skips if already there
  verify   --key KEY --file FILE   HEAD the object and compare size (and sha256 if known)
  presign  --key KEY               time-limited private URL for the LMS (default 1 h)
  selftest [--state]               check the R2 credentials end to end (write, read, presign, delete)
  cors                             apply config/r2-cors.json to the bucket (needs an Admin token)

Settings: R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET (in .env).
  --state uses STATE_R2_ACCOUNT_ID, STATE_R2_ACCESS_KEY_ID, STATE_R2_SECRET_ACCESS_KEY, STATE_BUCKET.
  R2_ENDPOINT_URL (optional) overrides https://<account>.r2.cloudflarestorage.com, e.g. for an
  EU-jurisdiction bucket (https://<account>.eu.r2.cloudflarestorage.com) or a local emulator.
Requires: pip install -r control/requirements.txt ; ffmpeg on PATH or FFMPEG_BIN.
How R2 works and how this script uses it: docs/R2.md

Fault tolerance
  * Encoding writes to *.part.mp4 and renames only after the output's duration is
    checked against the input, so a crash never leaves a "finished" broken file.
  * Re-running is safe: a valid encoded file is reused, and an object already in
    R2 with the same size and sha256 is not uploaded again.
  * Upload: 64 MB multipart parts, 4 in parallel, botocore adaptive retries, plus
    3 whole-upload attempts. The result is verified with HEAD before success.
  * The manifest (state/replays/<session>.json) is what `livestack.py stop` checks
    before destroying the server.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import ROOT, alert, load_env, log, require, retry, setup_logging  # noqa: E402

MANIFEST_DIR = ROOT / "state" / "replays"
MB = 1024 * 1024


# --------------------------------------------------------------------------- helpers
def safe_session(session_id: str) -> str:
    s = re.sub(r"[^A-Za-z0-9_-]+", "-", session_id).strip("-")
    return s or "session"


def default_key(session_id: str, when: dt.datetime | None = None) -> str:
    when = when or dt.datetime.now(dt.timezone.utc)
    return f"replays/{when:%Y}/{when:%m}/{safe_session(session_id)}.mp4"


def ffmpeg_bin() -> str:
    b = os.environ.get("FFMPEG_BIN") or shutil.which("ffmpeg")
    if not b:
        raise SystemExit("ffmpeg not found: add it to PATH or set FFMPEG_BIN in .env")
    return b


def ffprobe_bin() -> str:
    ff = Path(ffmpeg_bin())
    cand = ff.with_name(ff.name.replace("ffmpeg", "ffprobe"))
    return str(cand) if cand.exists() else (shutil.which("ffprobe") or "ffprobe")


def probe_duration(path: str | Path) -> float | None:
    try:
        r = subprocess.run([ffprobe_bin(), "-v", "error", "-show_entries", "format=duration",
                            "-of", "default=nw=1:nk=1", str(path)], capture_output=True, text=True, timeout=120)
        return float(r.stdout.strip()) if r.returncode == 0 and r.stdout.strip() not in ("", "N/A") else None
    except (ValueError, OSError, subprocess.TimeoutExpired):
        return None


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(8 * MB), b""):
            h.update(chunk)
    return h.hexdigest()


def manifest_path(session_id: str) -> Path:
    return MANIFEST_DIR / f"{safe_session(session_id)}.json"


def load_manifest(session_id: str) -> dict | None:
    try:
        return json.loads(manifest_path(session_id).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None


# --------------------------------------------------------------------------- encode
def encode(src: Path, dst: Path, crf: int = 23, preset: str = "medium", force: bool = False) -> Path:
    if not src.exists():
        raise SystemExit(f"input not found: {src}")
    if dst.exists() and not force and probe_duration(dst):
        log.info("encoded file already exists and is readable, reusing: %s (use --force to redo)", dst)
        return dst
    tmp = dst.with_name(dst.stem + ".part.mp4")
    cmd = [ffmpeg_bin(), "-hide_banner", "-y", "-i", str(src),
           "-c:v", "libx264", "-preset", preset, "-crf", str(crf), "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(tmp)]
    log.info("encoding %s -> %s", src, dst)
    subprocess.run(cmd, check=True)
    src_d, out_d = probe_duration(src), probe_duration(tmp)
    if not out_d:
        raise RuntimeError(f"encoded file is unreadable: {tmp}")
    if src_d and abs(src_d - out_d) > max(5.0, 0.01 * src_d):
        raise RuntimeError(f"duration mismatch: source {src_d:.1f}s vs output {out_d:.1f}s")
    os.replace(tmp, dst)
    log.info("encoded %.1f min, %.0f MB", out_d / 60, dst.stat().st_size / MB)
    return dst


# --------------------------------------------------------------------------- R2
def endpoint_url(account_id: str | None) -> str:
    override = os.environ.get("R2_ENDPOINT_URL", "").strip()
    if override:
        return override.rstrip("/")
    if not account_id:
        raise SystemExit("missing R2 account id in .env (R2 dashboard -> Overview -> Account ID)")
    return f"https://{account_id}.r2.cloudflarestorage.com"


def s3_client(state: bool = False):
    """S3 client for R2. state=False: replay bucket token (R2_*); True: state bucket token (STATE_R2_*)."""
    try:
        import boto3
        from botocore.config import Config
    except ImportError:
        raise SystemExit("boto3 is not installed: pip install -r control/requirements.txt") from None
    p = "STATE_R2_" if state else "R2_"
    key_id, secret = require(p + "ACCESS_KEY_ID", p + "SECRET_ACCESS_KEY")
    kw = dict(retries={"max_attempts": 10, "mode": "adaptive"}, connect_timeout=15, read_timeout=120)
    try:  # botocore >= 1.36 sends extra checksums by default; R2 only needs them when required
        cfg = Config(**kw, request_checksum_calculation="when_required", response_checksum_validation="when_required")
    except TypeError:
        cfg = Config(**kw)
    return boto3.client("s3", endpoint_url=endpoint_url(os.environ.get(p + "ACCOUNT_ID")),
                        aws_access_key_id=key_id, aws_secret_access_key=secret,
                        region_name="auto", config=cfg)


def bucket() -> str:
    return require("R2_BUCKET")[0]


def _code(e: Exception) -> str:
    return str(getattr(e, "response", {}).get("Error", {}).get("Code", type(e).__name__))


def head(s3, key: str) -> dict | None:
    from botocore.exceptions import ClientError
    try:
        return s3.head_object(Bucket=bucket(), Key=key)
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
            return None
        raise


def verify_object(key: str, expected_size: int, expected_sha: str | None = None, s3=None,
                  h: dict | None = None) -> tuple[bool, str]:
    s3 = s3 or s3_client()
    h = h if h is not None else head(s3, key)
    if h is None:
        return False, f"r2://{bucket()}/{key} does not exist"
    size = h["ContentLength"]
    if size != expected_size:
        return False, f"r2://{bucket()}/{key} is {size} bytes, expected {expected_size}"
    remote_sha = (h.get("Metadata") or {}).get("sha256")
    if expected_sha and remote_sha and remote_sha != expected_sha:
        return False, f"sha256 mismatch for {key}"
    if h.get("ContentType") != "video/mp4":
        log.warning("%s has Content-Type %s (expected video/mp4)", key, h.get("ContentType"))
    return True, f"r2://{bucket()}/{key} {size / MB:.1f} MB, size{' and sha256' if remote_sha and expected_sha else ''} match"


class _Progress:
    def __init__(self, total: int):
        self.total, self.done, self.next_pct, self.lock = total, 0, 5, threading.Lock()

    def __call__(self, n: int) -> None:
        with self.lock:
            self.done += n
            pct = self.done * 100 // max(self.total, 1)
            if pct >= self.next_pct:
                log.info("upload %d%% (%.1f / %.1f MB)", pct, self.done / MB, self.total / MB)
                self.next_pct = pct - pct % 5 + 5


def upload(file: Path, key: str, s3=None) -> dict:
    from boto3.s3.transfer import TransferConfig
    s3 = s3 or s3_client()
    size = file.stat().st_size
    log.info("hashing %s (%.0f MB)...", file.name, size / MB)
    digest = sha256_file(file)

    h = head(s3, key)
    ok, msg = verify_object(key, size, digest, s3, h=h) if h else (False, "")
    if ok and (h.get("Metadata") or {}).get("sha256") == digest:
        log.info("already uploaded (same size and sha256) - skipping: %s", msg)
    else:
        tc = TransferConfig(multipart_threshold=64 * MB, multipart_chunksize=64 * MB,
                            max_concurrency=4, use_threads=True)
        extra = {"ContentType": "video/mp4", "Metadata": {"sha256": digest}}
        log.info("uploading to r2://%s/%s", bucket(), key)
        retry(lambda: s3.upload_file(str(file), bucket(), key, ExtraArgs=extra, Config=tc, Callback=_Progress(size)),
              attempts=3, base_delay=10, what="upload")
        ok, msg = verify_object(key, size, digest, s3)
        if not ok:
            raise RuntimeError(f"upload verification failed: {msg}")
        log.info("verified: %s", msg)
    return {"bucket": bucket(), "key": key, "size": size, "sha256": digest,
            "uploaded_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")}


def presign(key: str, expires: int = 3600, s3=None) -> str:
    s3 = s3 or s3_client()
    return s3.generate_presigned_url("get_object", Params={"Bucket": bucket(), "Key": key}, ExpiresIn=expires)


def selftest(state: bool = False, s3=None) -> int:
    """End-to-end check of one R2 token against its bucket. Leaves nothing behind.

    Replay token (default): bucket reachable, write, read back, presigned URL works
    over plain HTTPS, delete, and (if STATE_BUCKET is set) it can NOT open the state
    bucket. --state: the same for the token the server uses for the TLS backup.
    """
    import urllib.request
    from botocore.exceptions import ClientError, EndpointConnectionError

    p = "STATE_R2_" if state else "R2_"
    bkt = require("STATE_BUCKET" if state else "R2_BUCKET")[0]
    other = os.environ.get("R2_BUCKET" if state else "STATE_BUCKET", "").strip()
    s3 = s3 or s3_client(state)
    key = f"selftest/{dt.datetime.now(dt.timezone.utc):%Y%m%dT%H%M%S}-{os.getpid()}.txt"
    body = f"livestack selftest {key}\n".encode()
    results: list[tuple[str, bool, str]] = []

    def check(name, fn):
        try:
            detail = fn()
            results.append((name, True, detail or "ok"))
            return True
        except (ClientError, EndpointConnectionError, OSError, AssertionError) as e:
            # HEAD responses have no body, so R2 then reports only the bare status code.
            missing = f"bucket '{bkt}' does not exist (check the name in .env / create it in the dashboard)"
            denied = "token not allowed on this bucket (check the token's bucket scope)"
            hint = {"InvalidAccessKeyId": f"wrong {p}ACCESS_KEY_ID",
                    "SignatureDoesNotMatch": f"wrong {p}SECRET_ACCESS_KEY",
                    "AccessDenied": denied, "NoSuchBucket": missing, "404": missing,
                    "403": f"wrong {p}ACCESS_KEY_ID / SECRET, or {denied}",
                    "400": f"bad request - check {p}ACCOUNT_ID (R2_ENDPOINT_URL if set)"}.get(_code(e), "")
            if isinstance(e, EndpointConnectionError):
                hint = f"cannot reach R2 - check {p}ACCOUNT_ID and your internet connection"
            results.append((name, False, f"{_code(e)}: {e}" + (f"  -> {hint}" if hint else "")))
            return False

    print(f"R2 selftest: bucket '{bkt}' at {s3.meta.endpoint_url} with {p}* credentials")
    if check("bucket reachable", lambda: s3.head_bucket(Bucket=bkt) and None):
        if check("write object", lambda: s3.put_object(Bucket=bkt, Key=key, Body=body, ContentType="text/plain") and key):
            def _read():
                got = s3.get_object(Bucket=bkt, Key=key)["Body"].read()
                assert got == body, "content differs"
                return f"{len(got)} bytes match"
            check("read back", _read)
            if not state:
                def _presigned():
                    url = s3.generate_presigned_url("get_object", Params={"Bucket": bkt, "Key": key}, ExpiresIn=120)
                    with urllib.request.urlopen(url, timeout=30) as r:
                        assert r.read() == body, "content differs"
                    return "plain HTTP GET with a 2-minute link works (this is what the LMS does)"
                check("presigned URL", _presigned)
            check("delete object", lambda: s3.delete_object(Bucket=bkt, Key=key) and "test object removed")
    if other:
        def _isolated():
            try:
                s3.head_bucket(Bucket=other)
            except ClientError as e:
                if _code(e) in ("403", "AccessDenied", "404", "NoSuchBucket", "Forbidden"):
                    return f"cannot open '{other}' ({_code(e)}) - good, the token is scoped to one bucket"
                raise
            return f"WARNING: this token CAN open '{other}'. Scope it to '{bkt}' only."
        check("token scope", _isolated)

    w = max(len(n) for n, _, _ in results)
    for n, ok, d in results:
        tag = "FAIL" if not ok else "WARN" if d.startswith("WARNING") else "PASS"
        print(f"  [{tag}] {n.ljust(w)}  {d}")
    failed = [n for n, ok, _ in results if not ok]
    print("R2 selftest: " + ("all checks passed" if not failed else f"{len(failed)} failed"))
    return 1 if failed else 0


# --------------------------------------------------------------------------- CLI
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--env-file")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("run", help="encode + upload + verify + manifest")
    s.add_argument("input", type=Path, help="session recording (mkv/mp4/...)")
    s.add_argument("--session-id", required=True, help="same id used for `livestack.py start --session`")
    s.add_argument("--key", help="override object key (default replays/YYYY/MM/<session>.mp4)")
    s.add_argument("--output", type=Path, help="encoded file path (default <input>.replay.mp4)")
    s.add_argument("--crf", type=int, default=23)
    s.add_argument("--preset", default="medium")
    s.add_argument("--no-encode", action="store_true", help="input is already a web-ready MP4")
    s.add_argument("--force", action="store_true", help="re-encode even if the output exists")

    s = sub.add_parser("encode"); s.add_argument("input", type=Path); s.add_argument("output", type=Path)
    s.add_argument("--crf", type=int, default=23); s.add_argument("--preset", default="medium")
    s.add_argument("--force", action="store_true")

    s = sub.add_parser("upload"); s.add_argument("file", type=Path); s.add_argument("--key", required=True)

    s = sub.add_parser("verify"); s.add_argument("--key", required=True)
    g = s.add_mutually_exclusive_group(required=True)
    g.add_argument("--file", type=Path); g.add_argument("--size", type=int)

    s = sub.add_parser("presign"); s.add_argument("--key", required=True)
    s.add_argument("--expires", type=int, default=3600)

    s = sub.add_parser("selftest", help="check R2 credentials end to end")
    s.add_argument("--state", action="store_true", help="test the STATE_R2_* token / STATE_BUCKET instead")

    s = sub.add_parser("cors"); s.add_argument("--file", type=Path, default=ROOT / "config" / "r2-cors.json")

    a = ap.parse_args(argv)
    setup_logging(a.verbose)
    load_env(a.env_file)

    try:
        if a.cmd == "encode":
            encode(a.input, a.output, a.crf, a.preset, a.force)
        elif a.cmd == "upload":
            print(json.dumps(upload(a.file, a.key), indent=2))
        elif a.cmd == "verify":
            size = a.file.stat().st_size if a.file else a.size
            digest = sha256_file(a.file) if a.file else None
            ok, msg = verify_object(a.key, size, digest)
            print(("OK  " if ok else "FAIL ") + msg)
            return 0 if ok else 1
        elif a.cmd == "presign":
            print(presign(a.key, a.expires))
        elif a.cmd == "selftest":
            return selftest(a.state)
        elif a.cmd == "cors":
            from botocore.exceptions import ClientError
            rules = json.loads(a.file.read_text(encoding="utf-8"))
            try:
                s3_client().put_bucket_cors(Bucket=bucket(), CORSConfiguration={"CORSRules": rules})
            except ClientError as e:
                if _code(e) in ("AccessDenied", "403"):
                    log.error("this token may not change bucket settings (Object Read & Write tokens can't). "
                              "Paste config/r2-cors.json in the dashboard instead: R2 -> %s -> Settings -> "
                              "CORS Policy -> Add, or use an Admin Read & Write token once.", bucket())
                    return 1
                raise
            log.info("CORS applied to %s from %s", bucket(), a.file)
        elif a.cmd == "run":
            out = a.input if a.no_encode else (a.output or a.input.with_name(a.input.stem + ".replay.mp4"))
            if not a.no_encode:
                encode(a.input, out, a.crf, a.preset, a.force)
            key = a.key or default_key(a.session_id)
            m = upload(out, key)
            m.update(session_id=a.session_id, source=str(a.input), file=str(out))
            MANIFEST_DIR.mkdir(parents=True, exist_ok=True)
            manifest_path(a.session_id).write_text(json.dumps(m, indent=2), encoding="utf-8")
            log.info("manifest written: %s", manifest_path(a.session_id))
            print(f"\nReplay ready: r2://{m['bucket']}/{m['key']}\n"
                  f"Now safe to run: python control/livestack.py stop")
    except (RuntimeError, subprocess.CalledProcessError) as e:
        log.error("%s", e)
        alert(f"replay {a.cmd} failed: {e}", "error")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
