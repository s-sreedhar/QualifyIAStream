#!/usr/bin/env python3
"""Qualify IAS recording uploader: encode a video on THIS computer and publish it as a multi-quality recording.

    python upload_recording.py "D:/Videos/Lecture 1.mp4" --title "Polity Lecture 1"
    python upload_recording.py "D:/Videos/Lecture 1.mp4" --link "https://.../api/v1/cms-video/upload?t=..."
    python upload_recording.py --check          (is everything installed?)

What it does
  1. Encodes 1080p / 720p / 480p / 360p adaptive HLS (only the sizes the video really has) and a poster picture, with ffmpeg.
  2. Asks for the UPLOAD LINK (made in the admin: LMS > Recordings > "Upload recording"; copy the address of the page that opens).
  3. Uploads every file straight to the recordings bucket with short-lived signed links from the backend, then registers the
     recording so it appears in the admin as READY.

This computer needs NO passwords or storage keys. The upload link is the only credential: it is valid for 72 hours and renews itself while the script runs and works for
that one recording only. Only Python 3.9+ and ffmpeg are required (no pip packages).
If anything stops half way, run the SAME command again: finished work is reused.
Safety: every uploaded file is written to a progress log at once (a power cut loses nothing that was already sent), internet problems
are retried for up to 30 minutes, expired storage links are refreshed, and the same file is never uploaded twice by accident
(a finished file is refused unless you add --force).
"""
from __future__ import annotations

import argparse
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
LADDER = [("1080p", 1080, 5000, 128), ("720p", 720, 2800, 128), ("480p", 480, 1400, 96), ("360p", 360, 800, 96)]   # 1080p only when the file is 1080p or larger
SEGMENT_SECONDS = 4
ENCODE_PRESET = "veryfast"
NET_ERRORS = (urllib.error.URLError, TimeoutError, ConnectionError, OSError)
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
    chosen = [r for r in LADDER if r[1] <= height + 8]   # never enlarge: a 480p file gets 480p and 360p
    return chosen or [("source", max(2, height - height % 2), 600, 64)]


def encode_rendition(src: Path, out_dir: Path, info: dict, height: int, vk: int, ak: int, on_progress):
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.iterdir():
        old.unlink()
    fps = min(info["fps"], 30.0)
    gop = max(2, round(fps * SEGMENT_SECONDS))
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-progress", "pipe:1", "-i", str(src),
           "-map", "0:v:0", "-map", "0:a:0?", "-vf", f"scale=-2:{height},fps={fps:.3f},format=yuv420p",
           "-c:v", "libx264", "-preset", ENCODE_PRESET, "-profile:v", "high" if height >= 720 else "main",
           "-b:v", f"{vk}k", "-maxrate", f"{int(vk * 1.07)}k", "-bufsize", f"{vk * 2}k",
           "-g", str(gop), "-keyint_min", str(gop), "-sc_threshold", "0", "-force_key_frames", f"expr:gte(t,n_forced*{SEGMENT_SECONDS})",
           "-c:a", "aac", "-b:a", f"{ak}k", "-ac", "2", "-ar", "48000",
           "-f", "hls", "-hls_time", str(SEGMENT_SECONDS), "-hls_playlist_type", "vod", "-hls_flags", "independent_segments",
           "-hls_segment_filename", str(out_dir / "seg_%05d.ts"), str(out_dir / "index.m3u8")]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    err: list[str] = []
    threading.Thread(target=lambda: err.extend(proc.stderr.readlines()), daemon=True).start()
    for line in proc.stdout:
        if line.startswith("out_time_us="):
            try:
                on_progress(min(1.0, int(line.split("=")[1]) / 1_000_000 / info["duration"]))
            except ValueError:
                pass
    if proc.wait() != 0 or not (out_dir / "index.m3u8").exists():
        raise RuntimeError("ffmpeg failed: " + "".join(err)[-400:])


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
    return "#EXT-X-ENDLIST" in text and bool(names) and all((folder / n).is_file() and (folder / n).stat().st_size > 0 for n in names)


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
                    die("The upload link is not valid any more (it lasts 72 hours). Make a new one in the admin: LMS > Recordings > Upload recording.")
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
        die("Paste the full address of the 'Upload recording' page (it starts with https://).")
    u = urllib.parse.urlparse(found.group(0))
    token = urllib.parse.parse_qs(u.query).get("t", [""])[0]
    if not token:
        die("That address has no upload code (?t=...). Copy the full address of the 'Upload recording' page.")
    return f"{u.scheme}://{u.netloc}", token


def check_server(api: "Api"):
    """Fail early, with a clear reason, when the link or the server is not ready (before hours of encoding)."""
    status = api.call("/status", soft=True)
    if status is None:
        die("The upload link is not valid. Make a new one in the admin: LMS > Recordings > Upload recording.")
    if not status.get("ok"):
        die(f"The upload link does not work: {status.get('message')}")
    if api.call("/hls/renew", {}, soft=True) is None:
        die("The server does not have the multi-quality upload feature yet. The BACKEND must be redeployed in Dokploy "
            "(it was updated on GitHub on 2026-11-23). Then run this again.")


def put_file(get_link, path: Path) -> int:
    """Upload one file. Retries lost connections patiently; if the signed link was refused or expired, asks for a fresh one."""
    size = path.stat().st_size
    refreshed, waited, attempt = 0, 0, 0
    fresh = False
    while True:
        try:
            url, headers = get_link(fresh)
            fresh = False
            req = urllib.request.Request(url, data=path.read_bytes(), method="PUT", headers={**headers, "User-Agent": UA})
            with urllib.request.urlopen(req, timeout=180) as r:
                if 200 <= r.status < 300:
                    return size
        except urllib.error.HTTPError as e:
            if e.code in (400, 401, 403):
                refreshed += 1
                if refreshed > 3:
                    raise RuntimeError(f"storage refused {path.name} ({e.code}) even with a fresh link")
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
    args = ap.parse_args()

    find_ffmpeg()
    if args.check:
        log("ffmpeg and ffprobe are installed.")
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

    # The fingerprint covers the file AND the encoding settings: after the script's ladder changes, old encodes are not reused.
    settings = f"{LADDER}|{ENCODE_PRESET}|{SEGMENT_SECONDS}"
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
    link = args.link or input("\nPaste the UPLOAD LINK from the admin (LMS > Recordings > Upload recording), then press Enter:\n> ")
    base, token = parse_link(link)
    api = Api(base, token)
    check_server(api)
    api.ping({"progress": 0, "title": title})

    # Progress log: one line per uploaded file ("name<TAB>size"), written the moment the file arrives. It belongs to THIS recording
    # (a new link makes a new recording, whose storage folder is empty, so an old log must not be reused for it).
    asset = asset_of(token)
    log_path = work / f"uploaded-{asset}.log"
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

    def upload_group(files: list[tuple[str, Path]]):
        """Upload files (several at once). Each finished file is logged immediately. Segments of a quality are always finished before its playlist."""
        todo = [f for f in files if done.get(f[0]) != f[1].stat().st_size]
        for i in range(0, len(todo), 100):
            batch = todo[i:i + 100]
            urls = api.call("/hls/urls", {"files": [n for n, _ in batch]})
            if not urls.get("ok"):
                raise RuntimeError(urls.get("message") or "could not get upload links")
            with lock:
                links.update(urls["urls"])
            with ThreadPoolExecutor(8) as pool:
                jobs = {pool.submit(put_file, (lambda fresh, _n=n: link_for(_n, fresh)), p): (n, p) for n, p in batch}
                for job in as_completed(jobs):
                    n, p = jobs[job]
                    sent = job.result()
                    with lock:
                        done[n] = sent
                        log_fh.write(f"{n}\t{sent}\n")
                        log_fh.flush()
                        stats["sent"] += sent
                        stats["files"] += 1

    def upload_quality(name: str):
        folder = work / "hls" / name
        upload_group([(f"hls/{name}/{p.name}", p) for p in sorted(folder.glob("seg_*.ts"))])
        upload_group([(f"hls/{name}/index.m3u8", folder / "index.m3u8")])
        log(f"[{name}] uploaded ({len(done)} files in storage so far, {stats['sent'] / 1048576:.0f} MB sent this run)")

    # ---- encode each quality (finished ones are reused); upload runs in the background meanwhile
    uploader = ThreadPoolExecutor(1)
    futures = []
    thumb = work / "thumb.jpg"
    if not thumb.exists():
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", str(max(1, int(info["duration"] * 0.1))), "-i", str(src),
                        "-frames:v", "1", "-vf", "scale=-2:360", "-y", str(thumb)], check=False)
    if thumb.exists():
        futures.append(uploader.submit(upload_group, [("thumb.jpg", thumb)]))
    # smaller qualities first: they finish sooner, so a usable video exists early
    for i, (name, height, vk, ak) in enumerate(sorted(ladder, key=lambda q: q[1])):
        folder = work / "hls" / name
        if rendition_ok(folder):
            log(f"[{name}] already encoded")
        else:
            last = [-1]

            def report(frac, _i=i, _n=name):
                pct = int(frac * 100)
                if pct >= last[0] + 10:
                    last[0] = pct
                    log(f"[{_n}] encoding {pct}%  (quality {_i + 1} of {len(ladder)})")
                    api.ping({"progress": min(90, int((_i + frac) * 90 / len(ladder)))})

            log(f"[{name}] encoding...")
            try:
                encode_rendition(src, folder, info, height, vk, ak, report)
            except RuntimeError as e:
                die(str(e))
            (folder / ".done").write_text("ok")
            if not rendition_ok(folder):
                die(f"The {name} version did not come out complete. Run the same command again.")
        futures.append(uploader.submit(upload_quality, name))
        for f in [f for f in futures if f.done()]:
            f.result()

    log("Encoding finished. Waiting for the uploads to finish...")
    try:
        for f in futures:
            f.result()
        master = ["#EXTM3U", "#EXT-X-VERSION:3"]
        order = {"480p": 0, "720p": 1, "360p": 2, "1080p": 3}
        for name, height, vk, ak in sorted(ladder, key=lambda q: order.get(q[0], 4)):
            width = max(2, round(info["width"] * height / max(info["height"], 1)) // 2 * 2)
            master += [f"#EXT-X-STREAM-INF:BANDWIDTH={(vk + ak) * 1000},RESOLUTION={width}x{height}", f"{name}/index.m3u8"]
        (work / "hls" / "master.m3u8").write_text("\n".join(master) + "\n")
        upload_group([("hls/master.m3u8", work / "hls" / "master.m3u8")])   # last: only now can the video be played
    except RuntimeError as e:
        die(f"{e}\nNothing is lost: run the same command again to continue.")
    finally:
        uploader.shutdown(wait=False)

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
    log(f"DONE. '{title}' is READY. Find it in the admin under LMS > Recordings, then attach it to a class or lesson.")
    if not args.keep:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped. Run the same command again to continue where it left off.")
        sys.exit(130)
