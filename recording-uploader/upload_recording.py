#!/usr/bin/env python3
"""Qualify IAS recording uploader: encode a video on THIS computer and publish it as a multi-quality recording.

    python upload_recording.py "D:/Videos/Lecture 1.mp4" --title "Polity Lecture 1"
    python upload_recording.py "D:/Videos/Lecture 1.mp4" --link "https://.../api/v1/cms-video/upload?t=..."
    python upload_recording.py --check          (is everything installed?)

What it does
  1. Asks for the UPLOAD LINK (made in the admin: LMS > Video Library > "Get local upload link"; press Copy).
  2. Encodes 720p and 480p adaptive HLS (only the sizes the video really has) and a poster picture, with ffmpeg, in ONE pass
     (the video is read and decoded once for both). Every piece is AES-128 encrypted with the recording's key (compulsory).
  3. Uploads every piece the moment it is encoded (12 at a time, reusing connections), straight to the private recordings bucket
     with short-lived signed links, then registers the recording so it appears in the admin as READY.

Faster uploads: lectures are mostly still pictures, so each quality is encoded to a quality level with a bitrate CAP instead of a
fixed bitrate; still scenes take far less space (typically 30-50% less to upload) and look the same.
  --encoder cpu|auto|nvenc|qsv|amf|videotoolbox
        cpu (default): the processor. Slower to encode than a graphics chip, but makes the SMALLEST files, and on most
        connections the upload, not the encode, is what takes longest. (Measured on a 3-minute 1080p test: processor 26 MB,
        AMD graphics chip 84 MB, in the same time.)
        auto: the first graphics-chip encoder that works on this PC (NVIDIA / Intel Quick Sync / AMD / Apple). Worth trying
        on a very fast upload line with a slow processor.
  --parallel N                               uploads at the same time (default 12; use 4 on a weak connection)

This computer needs NO passwords or storage keys. The upload link is the only credential: it is valid for 72 hours and renews itself while the script runs and works for
that one recording only. Only Python 3.9+ and ffmpeg are required (no pip packages).
If anything stops half way, run the SAME command again: finished work is reused.
Safety: every uploaded file is written to a progress log at once (a power cut loses nothing that was already sent), internet problems
are retried for up to 30 minutes, expired storage links are refreshed, and the same file is never uploaded twice by accident
(a finished file is refused unless you add --force).
"""
from __future__ import annotations

import argparse
import http.client
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

HERE = Path(__file__).resolve().parent
# name, height, video kbps, audio kbps (same ladder as the website's other recordings)
LADDER = [("720p", 720, 2800, 128), ("480p", 480, 1400, 96)]   # only these two qualities (a smaller file gets 480p, or its own size)
SEGMENT_SECONDS = 4
ENCODE_PRESET = "veryfast"
NET_ERRORS = (urllib.error.URLError, TimeoutError, ConnectionError, OSError, http.client.HTTPException)
UPLOAD_THREADS = 12
QUALITY_CRF = 23                  # processor encoder: quality level (lower = better/larger); the bitrate cap still applies

# Encoders, fastest first. Each gets the target bitrate (kbit/s) and returns its ffmpeg options. A hardware encoder is only used
# after a short test encode with exactly these options succeeds on this PC.
def _cap(vk: int) -> list[str]:
    return ["-maxrate", f"{int(vk * 1.07)}k", "-bufsize", f"{vk * 2}k"]

ENCODERS = {
    "nvenc": lambda vk, h: ["-c:v", "h264_nvenc", "-preset", "p5", "-tune", "hq", "-rc", "vbr", "-cq", "25", "-b:v", f"{vk}k", *_cap(vk),
                            "-forced-idr", "1", "-spatial-aq", "1", "-profile:v", "high"],
    "qsv": lambda vk, h: ["-c:v", "h264_qsv", "-preset", "medium", "-b:v", f"{vk}k", *_cap(vk), "-forced_idr", "1", "-profile:v", "high"],
    "amf": lambda vk, h: ["-c:v", "h264_amf", "-quality", "balanced", "-rc", "qvbr", "-qvbr_quality_level", "23", "-b:v", f"{vk}k", *_cap(vk),
                          "-profile:v", "high"],
    "videotoolbox": lambda vk, h: ["-c:v", "h264_videotoolbox", "-b:v", f"{vk}k", *_cap(vk), "-profile:v", "high", "-allow_sw", "0"],
    "cpu": lambda vk, h: ["-c:v", "libx264", "-preset", ENCODE_PRESET, "-crf", str(QUALITY_CRF), *_cap(vk), "-sc_threshold", "0",
                          "-profile:v", "high" if h >= 720 else "main"],
}
HW_ORDER = {"nt": ["nvenc", "qsv", "amf"], "darwin": ["videotoolbox"], "linux": ["nvenc", "qsv"]}
MAX_OUTAGE_SECONDS = 30 * 60      # keep retrying a lost connection this long before stopping (a re-run continues from the log)
UA = "Mozilla/5.0 (compatible; QualifyIAS-Uploader/1.0)"
API = "/api/v1/cms-video"


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def die(message: str):
    print(f"\nERROR: {message}", file=sys.stderr)
    sys.exit(1)


# ----------------------------------------------------------------------------------------------------- ffmpeg
def find_ffmpeg():
    extra = [os.environ.get("FFMPEG_DIR", ""), str(HERE / "ffmpeg" / "bin"), str(HERE / "ffmpeg")]
    local = os.environ.get("LOCALAPPDATA")
    if local and (Path(local) / "Microsoft" / "WinGet" / "Packages").is_dir():
        extra += [str(p.parent) for p in (Path(local) / "Microsoft" / "WinGet" / "Packages").rglob("ffmpeg.exe")]
    extra += [r"C:\ffmpeg\bin", r"C:\Program Files\ffmpeg\bin", "/opt/homebrew/bin", "/usr/local/bin"]
    extra += [str(p) for p in sorted(Path.home().glob("ffmpeg*/bin"), reverse=True)]   # e.g. ~/ffmpeg-9.0.2-essentials_build/bin
    for folder in extra:
        if folder and (Path(folder) / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg")).is_file():
            os.environ["PATH"] = folder + os.pathsep + os.environ.get("PATH", "")
            break
    if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
        die("ffmpeg / ffprobe not found. Install it (Windows: winget install Gyan.FFmpeg, Mac: brew install ffmpeg, "
            "Linux: sudo apt install ffmpeg), then open a NEW terminal. Or set FFMPEG_DIR to its bin folder.")


def probe(path: Path) -> dict:
    out = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format", str(path)],
                         check=True, capture_output=True, text=True).stdout
    data = json.loads(out)
    video = next((s for s in data["streams"] if s.get("codec_type") == "video"), None)
    if not video:
        die("The file has no video track.")
    num, _, den = (video.get("avg_frame_rate") or "30/1").partition("/")
    fps = float(num) / float(den or 1) if float(den or 1) else 30.0
    duration = float(data.get("format", {}).get("duration") or video.get("duration") or 0)
    if duration <= 0:
        die("Could not read the video's length. Is the file complete?")
    return {"width": int(video["width"]), "height": int(video["height"]), "fps": fps if 1 < fps < 120 else 30.0, "duration": duration}


def ladder_for(height: int):
    chosen = [r for r in LADDER if r[1] <= height + 8]   # never enlarge: a 480p file gets only 480p
    return chosen or [("source", max(2, height - height % 2), 600, 64)]


def encoder_works(name: str) -> bool:
    """A 2-second test encode with the real options (hardware may be listed by ffmpeg but missing or busy on this PC)."""
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30",
           "-t", "2", "-vf", "format=yuv420p", *ENCODERS[name](1500, 720), "-g", "60", "-keyint_min", "60",
           "-force_key_frames", "expr:gte(t,n_forced*2)", "-f", "null", "-"]
    try:
        return subprocess.run(cmd, capture_output=True, timeout=60).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def pick_encoder(wanted: str) -> str:
    if wanted != "auto":
        if wanted not in ENCODERS:
            die(f"Unknown encoder '{wanted}'. Use one of: auto, {', '.join(ENCODERS)}.")
        if wanted != "cpu" and not encoder_works(wanted):
            die(f"The {wanted} encoder does not work on this PC. Leave --encoder out to use the processor.")
        return wanted
    system = "darwin" if sys.platform == "darwin" else ("nt" if os.name == "nt" else "linux")
    for name in HW_ORDER.get(system, []):
        if encoder_works(name):
            return name
    return "cpu"


def write_key_info(work: Path, key_hex: str, uri: str) -> Path:
    """ffmpeg -hls_key_info_file: line 1 = the key's address written in the playlist (the server replaces it with the signed key link),
    line 2 = the local key file. No IV line: each piece then uses its sequence number as IV, which is what the player expects.
    The key file stays in the work folder (deleted with it) and is never uploaded."""
    key = bytes.fromhex(key_hex)
    if len(key) != 16:
        die("The server sent an invalid encryption key.")
    key_file = work / "aes128.key"
    key_file.write_bytes(key)
    info_file = work / "aes128.keyinfo"
    info_file.write_text(f"{uri}\n{key_file}\n", encoding="utf-8")
    return info_file


def encode_all(src: Path, hls_dir: Path, info: dict, ladder, encoder: str, on_progress, key_info: Path) -> subprocess.Popen:
    """Starts ONE ffmpeg that decodes the video once and writes every quality (hls/<name>/seg_00000.ts ...). Returns the process.
    Every piece is AES-128 encrypted with this recording's key, so the pieces are useless to anyone without the key."""
    fps = min(info["fps"], 30.0)
    gop = max(2, round(fps * SEGMENT_SECONDS))
    n = len(ladder)
    graph = f"[0:v:0]fps={fps:.3f},format=yuv420p,split={n}" + "".join(f"[s{k}]" for k in range(n)) + ";"
    graph += ";".join(f"[s{k}]scale=-2:{q[1]}[v{k}]" for k, q in enumerate(ladder))
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-progress", "pipe:1", "-i", str(src), "-filter_complex", graph]
    for k, (name, height, vk, ak) in enumerate(ladder):
        out = hls_dir / name
        out.mkdir(parents=True, exist_ok=True)
        for old in out.iterdir():
            old.unlink()
        cmd += ["-map", f"[v{k}]", "-map", "0:a:0?", *ENCODERS[encoder](vk, height),
                "-g", str(gop), "-keyint_min", str(gop), "-force_key_frames", f"expr:gte(t,n_forced*{SEGMENT_SECONDS})",
                "-c:a", "aac", "-b:a", f"{ak}k", "-ac", "2", "-ar", "48000",
                "-f", "hls", "-hls_time", str(SEGMENT_SECONDS), "-hls_playlist_type", "vod", "-hls_flags", "independent_segments",
                "-hls_key_info_file", str(key_info),
                "-hls_segment_filename", str(out / "seg_%05d.ts"), str(out / "index.m3u8")]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    proc.err_lines = []  # type: ignore[attr-defined]
    threading.Thread(target=lambda: proc.err_lines.extend(proc.stderr.readlines()), daemon=True).start()  # type: ignore[attr-defined]

    def read_progress():
        for line in proc.stdout:
            if line.startswith("out_time_us="):
                try:
                    on_progress(min(1.0, int(line.split("=")[1]) / 1_000_000 / info["duration"]))
                except ValueError:
                    pass
    threading.Thread(target=read_progress, daemon=True).start()
    return proc


def segments_aligned(hls_dir: Path, ladder) -> bool:
    """Every quality must cut its pieces at the same moments, or switching quality while playing glitches."""
    lists = []
    for name, *_ in ladder:
        text = (hls_dir / name / "index.m3u8").read_text(errors="ignore")
        lists.append([float(x) for x in re.findall(r"#EXTINF:([\d.]+)", text)])
    first = lists[0]
    return all(len(other) == len(first) and all(abs(a - b) < 0.12 for a, b in zip(first, other)) for other in lists[1:])


# ----------------------------------------------------------------------------------------------------- web
def backoff(attempt: int) -> int:
    """3, 6, 12, 24, 48, then 60 seconds between tries."""
    return min(60, 3 * (2 ** min(attempt, 5)))


def rendition_ok(folder: Path) -> bool:
    """An encoded quality is only trusted when it was finished AND its playlist and every segment are really on disk."""
    playlist = folder / "index.m3u8"
    if not (folder / ".done").exists() or not playlist.exists():
        return False
    text = playlist.read_text(errors="ignore")
    names = re.findall(r"^(seg_\d+\.ts)$", text, re.M)
    return "#EXT-X-ENDLIST" in text and "#EXT-X-KEY:METHOD=AES-128" in text and bool(names) and all((folder / n).is_file() and (folder / n).stat().st_size > 0 for n in names)


def asset_of(token: str) -> str:
    """The recording this upload link belongs to (the link carries it); progress is kept per recording."""
    try:
        body = token.split(".")[0]
        claims = json.loads(__import__("base64").urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        return re.sub(r"[^A-Za-z0-9-]", "", str(claims.get("a", "")))[:40] or "unknown"
    except Exception:
        return "unknown"


class Api:
    def __init__(self, base: str, token: str):
        self.base, self.token = base.rstrip("/"), token
        self.renewed = time.time()

    def _once(self, path: str, body: dict | None, timeout: int = 60):
        data = json.dumps(body).encode() if body is not None else None
        headers = {"User-Agent": UA, "x-upload-token": self.token}
        if data is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base + API + path, data=data, headers=headers, method="POST" if data is not None else "GET")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)

    def renew(self):
        """Swap the link's code for a fresh 72-hour one (called hourly), so the link never runs out during a long job."""
        self.renewed = time.time()
        res = self.call("/hls/renew", {})
        if res.get("ok") and res.get("token"):
            self.token = res["token"]

    def ping(self, body: dict):
        """Progress for the admin screen. Best effort: a slow or lost connection must never stop an encode or an upload."""
        try:
            if time.time() - self.renewed > 3000:
                res = self._once("/hls/renew", {}, 15)
                self.renewed = time.time()
                if res.get("ok") and res.get("token"):
                    self.token = res["token"]
            self._once("/hls/progress", body, 15)
        except Exception:
            pass

    def call(self, path: str, body: dict | None = None, method: str | None = None, soft: bool = False):
        """A request that survives internet outages: retries for up to 30 minutes, then stops with a message (re-running continues)."""
        if path != "/hls/renew" and time.time() - self.renewed > 3000:
            self.renew()
        attempt, waited = 0, 0
        while True:
            try:
                return self._once(path, body)
            except urllib.error.HTTPError as e:
                if soft and e.code in (401, 403, 404):
                    return None
                if e.code in (401, 403):
                    die("The upload link is not valid any more (it lasts 72 hours). Make a new one in the admin: LMS > Video Library > Get local upload link.")
                if e.code < 500 and e.code != 429:
                    die(f"The server refused the request ({e.code}).")
            except (*NET_ERRORS, json.JSONDecodeError):
                pass
            wait = backoff(attempt)
            attempt += 1
            waited += wait
            if waited > MAX_OUTAGE_SECONDS:
                die("The server could not be reached for 30 minutes. Nothing is lost: when the internet is back, run the same command again.")
            log(f"Connection problem. Trying again in {wait} s (will keep trying for up to {(MAX_OUTAGE_SECONDS - waited + wait) // 60} more minutes)...")
            time.sleep(wait)


def parse_link(text: str) -> tuple[str, str]:
    """Accepts the address as copied from the browser, or the whole sentence/markdown that contains it."""
    found = re.search(r"https?://[^\s)\]\"'<>]+", text or "")
    if not found:
        die("Paste the link from Admin > LMS > Video Library > 'Get local upload link' (it starts with https://).")
    u = urllib.parse.urlparse(found.group(0))
    token = urllib.parse.parse_qs(u.query).get("t", [""])[0]
    if not token:
        die("That address has no upload code (?t=...). Use the Copy button in Admin > LMS > Video Library > 'Get local upload link'.")
    return f"{u.scheme}://{u.netloc}", token


def check_server(api: "Api"):
    """Fail early, with a clear reason, when the link or the server is not ready (before hours of encoding)."""
    status = api.call("/status", soft=True)
    if status is None:
        die("The upload link is not valid. Make a new one in the admin: LMS > Video Library > Get local upload link.")
    if not status.get("ok"):
        die(f"The upload link does not work: {status.get('message')}")
    if api.call("/hls/renew", {}, soft=True) is None:
        die("The server does not have the multi-quality upload feature yet. The BACKEND must be redeployed in Dokploy "
            "(it was updated on GitHub on 2026-11-23). Then run this again.")
    key = api.call("/hls/key", {}, soft=True)
    if key is None:
        die("The server cannot encrypt recordings yet (added 2026-11-25). The BACKEND must be redeployed in Dokploy, then run this again.")
    if not key.get("ok") or not key.get("key"):
        die(f"The server cannot encrypt recordings: {key.get('message')}")
    return key


_conns = threading.local()


def http_put(url: str, body: bytes, headers: dict, timeout: int = 180) -> int:
    """PUT over a kept-open connection (one per upload thread): no new secure handshake for every 2 MB piece."""
    u = urllib.parse.urlsplit(url)
    cache = getattr(_conns, "c", None)
    if cache is None:
        cache = _conns.c = {}
    key = (u.scheme, u.netloc)
    conn = cache.get(key)
    if conn is None:
        conn = (http.client.HTTPSConnection if u.scheme == "https" else http.client.HTTPConnection)(u.netloc, timeout=timeout)
        cache[key] = conn
    try:
        conn.request("PUT", u.path + ("?" + u.query if u.query else ""), body=body, headers={**headers, "User-Agent": UA})
        r = conn.getresponse()
        r.read()
        return r.status
    except Exception:
        conn.close()
        cache.pop(key, None)
        raise


def put_file(get_link, path: Path) -> int:
    """Upload one file. Retries lost connections patiently; if the signed link was refused or expired, asks for a fresh one."""
    size = path.stat().st_size
    body = path.read_bytes()
    refreshed, waited, attempt = 0, 0, 0
    fresh = False
    while True:
        try:
            url, headers = get_link(fresh)
            fresh = False
            status = http_put(url, body, headers)
            if 200 <= status < 300:
                return size
            if status in (400, 401, 403):
                refreshed += 1
                if refreshed > 3:
                    raise RuntimeError(f"storage refused {path.name} ({status}) even with a fresh link")
                fresh = True
                continue
        except NET_ERRORS:
            pass
        wait = backoff(attempt)
        attempt += 1
        waited += wait
        if waited > MAX_OUTAGE_SECONDS:
            raise RuntimeError(f"could not upload {path.name} (connection lost for 30 minutes)")
        time.sleep(wait)


# ----------------------------------------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("video", nargs="?", help="the video file")
    ap.add_argument("--title", help="title shown to students (default: the file name)")
    ap.add_argument("--link", help="the upload link from the admin (asked for after encoding if missing)")
    ap.add_argument("--work", help="folder for the encoded files (default: ./work next to this script)")
    ap.add_argument("--keep", action="store_true", help="keep the encoded files after a successful upload")
    ap.add_argument("--force", action="store_true", help="upload again even if this exact file was already uploaded")
    ap.add_argument("--check", action="store_true", help="check ffmpeg and (with --link) the link, then stop")
    ap.add_argument("--encoder", default="cpu", help="cpu (default: smallest upload), auto (graphics chip), nvenc, qsv, amf or videotoolbox")
    ap.add_argument("--parallel", type=int, default=UPLOAD_THREADS, help=f"uploads at the same time (default {UPLOAD_THREADS})")
    args = ap.parse_args()

    find_ffmpeg()
    if args.check:
        log("ffmpeg and ffprobe are installed.")
        log(f"Graphics-chip encoder on this PC: {pick_encoder('auto')} (used only with --encoder auto; the default is the processor)")
        if args.link:
            base, token = parse_link(args.link)
            check_server(Api(base, token))
            log("Upload link is valid and the server is ready for uploads.")
        return

    raw = args.video or input("Drag the video file here, then press Enter: ")
    src = Path(re.sub(r"^&\s+", "", raw.strip()).strip('"').strip("'")).expanduser().resolve()
    if not src.is_file():
        die(f"File not found: {src}")
    title = (args.title or src.stem).strip()[:200]
    if len(title) < 3:
        die("The title needs at least 3 characters.")

    info = probe(src)
    ladder = ladder_for(info["height"])
    size = src.stat().st_size
    log(f"{src.name}: {info['width']}x{info['height']}, {int(info['duration'] // 60)} min, {size / 1048576:.0f} MB -> qualities {[q[0] for q in ladder]}")

    encoder = pick_encoder(args.encoder)
    log(f"Encoder: {encoder}" + (" (processor: smallest files to upload)" if encoder == "cpu" else " (graphics chip)"))
    # The fingerprint covers the file AND the encoding settings: after the script's ladder changes, old encodes are not reused.
    settings = f"{LADDER}|{ENCODE_PRESET}|{SEGMENT_SECONDS}|{QUALITY_CRF}|onepass"
    fingerprint = hashlib.sha1(f"{src}|{size}|{int(src.stat().st_mtime)}|{settings}".encode()).hexdigest()[:10]
    completed_file = HERE / "completed.json"
    try:
        completed = json.loads(completed_file.read_text()) if completed_file.exists() else {}
    except (ValueError, OSError):
        completed = {}
    if fingerprint in completed and not args.force:
        c = completed[fingerprint]
        log(f"This exact file was already uploaded on {c.get('at')} as '{c.get('title')}'. Nothing to do. "
            f"(Add --force only if you really want a second copy.)")
        return
    work = Path(args.work or HERE / "work").resolve() / f"{re.sub(r'[^A-Za-z0-9_-]+', '-', src.stem)[:30]}-{fingerprint}"
    (work / "hls").mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(work).free < size * 1.5:
        log("WARNING: little free disk space; the encoded copies need roughly the size of the original.")

    # ---- the upload link comes FIRST so each quality can upload while the next one is still encoding (saves the upload time)
    link = args.link or input("\nPaste the UPLOAD LINK from the admin (LMS > Video Library > Get local upload link), then press Enter:\n> ")
    base, token = parse_link(link)
    api = Api(base, token)
    enc = check_server(api)
    api.ping({"progress": 0, "title": title})

    # Progress log: one line per uploaded file ("name<TAB>size"), written the moment the file arrives. It belongs to THIS recording
    # (a new link makes a new recording, whose storage folder is empty, so an old log must not be reused for it).
    asset = asset_of(token)
    # Pieces are encrypted with THIS recording's key. An earlier encode of the same file for another recording (another link) used a
    # different key, so it must not be reused: forget it and encode again.
    marker = work / "hls" / ".key-for"
    if marker.exists() and marker.read_text(errors="ignore").strip() != asset:
        log("The encoded files in the work folder belong to another recording (another upload link); encoding again.")
        for done_flag in (work / "hls").glob("*/.done"):
            done_flag.unlink()
    marker.write_text(asset)
    key_info = write_key_info(work, enc["key"], enc.get("uri") or "__key__.bin")
    # "-enc": a log from an older, unencrypted run of this script must never make encrypted pieces look already uploaded.
    log_path = work / f"uploaded-{asset}-enc.log"
    done: dict[str, int] = {}
    if log_path.exists():
        for line in log_path.read_text(errors="ignore").splitlines():
            name, _, sz = line.partition("\t")
            if name and sz.isdigit():          # a half-written last line (power cut) is simply ignored
                done[name] = int(sz)
    if done:
        log(f"Resuming: {len(done)} files were already uploaded to this recording and will be skipped.")
    log_fh = open(log_path, "a", encoding="utf-8")
    lock = threading.Lock()
    stats = {"sent": 0, "files": 0}
    links: dict[str, dict] = {}

    def link_for(name: str, fresh: bool = False):
        with lock:
            if not fresh and name in links:
                return links[name]["url"], links[name]["headers"]
        got = api.call("/hls/urls", {"files": [name]})
        if not got.get("ok"):
            raise RuntimeError(got.get("message") or "could not get an upload link")
        with lock:
            links[name] = got["urls"][name]
        return got["urls"][name]["url"], got["urls"][name]["headers"]

    # ---- ONE ffmpeg encodes every quality; each finished piece is uploaded while the rest is still being encoded
    pool = ThreadPoolExecutor(max(1, min(32, args.parallel)))
    futures: list = []
    queued: set[str] = set()
    started_at = time.time()

    def queue(files: list[tuple[str, Path]]):
        new = [(n, p) for n, p in files if n not in queued and done.get(n) != p.stat().st_size]
        for k in range(0, len(new), 100):
            batch = new[k:k + 100]
            urls = api.call("/hls/urls", {"files": [n for n, _ in batch]})
            if not urls.get("ok"):
                raise RuntimeError(urls.get("message") or "could not get upload links")
            with lock:
                links.update(urls["urls"])
            for n, path in batch:
                queued.add(n)
                futures.append(pool.submit(upload_one, n, path))
        for n, _ in files:
            queued.add(n)

    def upload_one(n: str, path: Path):
        sent = put_file(lambda fresh, _n=n: link_for(_n, fresh), path)
        with lock:
            done[n] = sent
            log_fh.write(f"{n}\t{sent}\n")
            log_fh.flush()
            stats["sent"] += sent
            stats["files"] += 1

    def wait_uploads():
        for f in as_completed(list(futures)):
            f.result()
        futures.clear()

    hls_dir = work / "hls"
    thumb = work / "thumb.jpg"
    if not thumb.exists():
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", str(max(1, int(info["duration"] * 0.1))), "-i", str(src),
                        "-frames:v", "1", "-vf", "scale=-2:360", "-y", str(thumb)], check=False)
    try:
        if thumb.exists():
            queue([("thumb.jpg", thumb)])
        all_ok = all(rendition_ok(hls_dir / name) for name, *_ in ladder)
        if all_ok:
            log("Every quality is already encoded; uploading what is missing.")

        def back_to_cpu(reason: str):
            """Re-encode on the processor; every piece is sent again (the new pieces replace the old ones in storage)."""
            log(reason)
            wait_uploads()
            with lock:
                for n in [n for n in done if n.startswith("hls/")]:
                    done.pop(n)
            queued.difference_update([n for n in queued if n.startswith("hls/")])

        while not all_ok:
            last = [-1]

            def report(frac):
                pct = int(frac * 100)
                if pct >= last[0] + 5:
                    last[0] = pct
                    mb = stats["sent"] / 1048576
                    log(f"encoding {pct}% (all qualities at once, {encoder})  ·  uploaded so far: {stats['files']} pieces, {mb:.0f} MB")
                    api.ping({"progress": min(90, int(frac * 90))})

            log(f"Encoding {', '.join(q[0] for q in ladder)} in one pass with {encoder}; pieces upload as soon as they are ready...")
            proc = encode_all(src, hls_dir, info, ladder, encoder, report, key_info)
            while proc.poll() is None:
                time.sleep(2)
                ready = []
                for name, *_ in ladder:
                    segs = sorted((hls_dir / name).glob("seg_*.ts"))
                    ready += [(f"hls/{name}/{p.name}", p) for p in segs[:-1]]   # the newest piece may still be being written
                if ready:
                    queue(ready)
                for f in [f for f in futures if f.done()]:
                    f.result()                                                    # an upload that failed for good stops the run
            if proc.returncode != 0:
                if encoder != "cpu":
                    back_to_cpu(f"The {encoder} encoder stopped with an error; encoding again on the processor.")
                    encoder = "cpu"
                    continue
                die("ffmpeg failed: " + "".join(getattr(proc, "err_lines", []))[-400:])
            for name, *_ in ladder:
                (hls_dir / name / ".done").write_text("ok")
            if not all(rendition_ok(hls_dir / name) for name, *_ in ladder):
                die("The encoded video did not come out complete. Run the same command again.")
            if encoder != "cpu" and not segments_aligned(hls_dir, ladder):
                # Rare with hardware encoders: qualities cut at different moments. Re-encode on the processor and re-send the pieces.
                back_to_cpu(f"The {encoder} encoder did not cut every quality at the same moments; encoding again on the processor.")
                encoder = "cpu"
                continue
            all_ok = True

        for name, *_ in ladder:
            queue([(f"hls/{name}/{p.name}", p) for p in sorted((hls_dir / name).glob("seg_*.ts"))])
        log("Encoding finished. Waiting for the last uploads...")
        wait_uploads()
        for name, *_ in ladder:                                                   # playlists only after all their pieces
            queue([(f"hls/{name}/index.m3u8", hls_dir / name / "index.m3u8")])
        wait_uploads()
        master = ["#EXTM3U", "#EXT-X-VERSION:3"]
        order = {"480p": 0, "720p": 1, "360p": 2, "1080p": 3}
        for name, height, vk, ak in sorted(ladder, key=lambda q: order.get(q[0], 4)):
            width = max(2, round(info["width"] * height / max(info["height"], 1)) // 2 * 2)
            master += [f"#EXT-X-STREAM-INF:BANDWIDTH={(int(vk * 1.07) + ak) * 1000},RESOLUTION={width}x{height}", f"{name}/index.m3u8"]
        (hls_dir / "master.m3u8").write_text("\n".join(master) + "\n")
        queue([("hls/master.m3u8", hls_dir / "master.m3u8")])                     # last: only now can the video be played
        wait_uploads()
    except RuntimeError as e:
        die(f"{e}\nNothing is lost: run the same command again to continue.")
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    secs = max(1, time.time() - started_at)
    log(f"Uploaded {stats['sent'] / 1048576:.0f} MB in {int(secs // 60)} min {int(secs % 60)} s "
        f"({stats['sent'] * 8 / secs / 1_000_000:.1f} Mbit/s average while encoding and uploading).")

    res = api.call("/hls/finish", {
        "durationSeconds": int(info["duration"]), "sizeBytes": size, "hasThumbnail": thumb.exists(),
        "renditions": [{"name": n, "height": h, "bitrate_kbps": vk + ak} for n, h, vk, ak in ladder],
    })
    if not res.get("ok"):
        die(f"Upload finished but could not be registered: {res.get('message')}\nRun the same command again.")
    try:
        completed[fingerprint] = {"title": title, "asset": asset, "at": time.strftime("%Y-%m-%d %H:%M")}
        completed_file.write_text(json.dumps(completed, indent=1))
    except OSError:
        pass
    log_fh.close()
    log(f"DONE. '{title}' is READY. Find it in the admin under LMS > Video Library; add it to a batch section from Batch Recordings & Tree.")
    if not args.keep:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped. Run the same command again to continue where it left off.")
        sys.exit(130)
