#!/usr/bin/env python3
"""Local stand-in for the nginx origin (config/nginx-origin.conf).

Serves the FFmpeg ladder output with the same URL layout and headers as production,
so the player, probe.py and a tunnel behave as they will behind the CDN:

  /                          list of live streams with "watch" links
  /player.html               local/player.html (hls.js test player)
  /hls/<path>/master.m3u8    ladder output, e.g. /hls/live/test/master.m3u8
  /healthz                   "ok"

Headers (same as nginx-origin.conf):
  .m3u8  application/vnd.apple.mpegurl   Cache-Control: no-cache
  .ts    video/mp2t                       Cache-Control: public, max-age=60
  Access-Control-Allow-Origin: *          only GET / HEAD / OPTIONS

Usage: python local/hls_server.py --root local/.live/hls --port 8090
Stdlib only. Not hardened for the open internet - for testing (including via a
temporary tunnel) only.
"""
from __future__ import annotations

import argparse
import html
import os
import posixpath
import sys
import time
import urllib.parse
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
PLAYER = HERE / "player.html"

TYPES = {
    ".m3u8": ("application/vnd.apple.mpegurl", "no-cache"),
    ".ts": ("video/mp2t", "public, max-age=60"),
    ".m4s": ("video/iso.segment", "public, max-age=60"),
    ".mp4": ("video/mp4", "public, max-age=60"),
}


class Handler(SimpleHTTPRequestHandler):
    server_version = "livestack-local-origin"
    hls_root: Path = Path(".")

    # ---------------------------------------------------------------- routing
    def do_GET(self):
        self._route(head=False)

    def do_HEAD(self):
        self._route(head=True)

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.send_header("Access-Control-Max-Age", "86400")
        self.end_headers()

    def _deny(self):
        self.send_error(405, "Method Not Allowed")

    do_POST = do_PUT = do_DELETE = do_PATCH = _deny

    def _route(self, head: bool):
        path = urllib.parse.urlsplit(self.path).path
        if path == "/healthz":
            return self._text("ok\n", "text/plain", head)
        if path in ("/", "/index.html"):
            return self._text(self._index(), "text/html; charset=utf-8", head)
        if path == "/player.html":
            return self._file(PLAYER, "text/html; charset=utf-8", "no-cache", head)
        if path.startswith("/hls/"):
            target = self._safe(path[len("/hls/"):])
            if target and target.is_file():
                ctype, cache = TYPES.get(target.suffix.lower(), ("application/octet-stream", "no-cache"))
                return self._file(target, ctype, cache, head)
        self.send_error(404, "Not Found")

    # ---------------------------------------------------------------- helpers
    def _safe(self, rel: str) -> Path | None:
        """Map a URL path to a file under hls_root; refuse anything that escapes it."""
        rel = posixpath.normpath(urllib.parse.unquote(rel)).lstrip("/")
        if rel.startswith("..") or "\\" in rel or ":" in rel:
            return None
        p = (self.hls_root / rel).resolve()
        return p if p.is_relative_to(self.hls_root) else None

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, HEAD, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Range")
        self.send_header("Access-Control-Expose-Headers", "Content-Length, Content-Range")

    def _text(self, body: str, ctype: str, head: bool):
        data = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self._cors()
        self.end_headers()
        if not head:
            self.wfile.write(data)

    def _file(self, p: Path, ctype: str, cache: str, head: bool):
        # FFmpeg writes *.tmp and renames, but the delete of an old segment can race
        # with a request; treat a vanished file as 404 instead of a 500.
        try:
            with p.open("rb") as f:
                data = f.read()
        except (FileNotFoundError, PermissionError):
            return self.send_error(404, "Not Found")
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", cache)
        self._cors()
        self.end_headers()
        if not head:
            self.wfile.write(data)

    def _index(self) -> str:
        rows = []
        for m in sorted(self.hls_root.rglob("master.m3u8")):
            rel = m.parent.relative_to(self.hls_root).as_posix()
            age = time.time() - m.stat().st_mtime
            fresh = any(time.time() - p.stat().st_mtime < 15 for p in m.parent.glob("*/index.m3u8"))
            state = "LIVE" if fresh else f"stopped ({int(age)} s ago)"
            url = f"/hls/{rel}/master.m3u8"
            rows.append(f'<li><b>{html.escape(rel)}</b> - {state} - '
                        f'<a href="/player.html?src={urllib.parse.quote(url)}">watch</a> - '
                        f'<a href="{html.escape(url)}">master.m3u8</a></li>')
        body = "\n".join(rows) or "<li>No streams yet. Start streaming from OBS, then refresh.</li>"
        return ("<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>"
                "<title>Local origin</title><body style='font:16px system-ui;padding:16px'>"
                f"<h1>Local origin</h1><ul>{body}</ul>"
                "<p style='color:#666'>Same layout as https://origin.example.com/hls/... in production.</p>")

    # Players open and drop connections all the time; don't print tracebacks for that.
    def handle_one_request(self):
        try:
            super().handle_one_request()
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
            self.close_connection = True

    def log_message(self, fmt, *args):
        if self.server.verbose or (len(args) > 1 and str(args[1])[:1] in "45"):
            sys.stderr.write("%s %s\n" % (self.log_date_time_string(), fmt % args))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", required=True, help="HLS output folder (HLS_ROOT of the ladder)")
    ap.add_argument("--port", type=int, default=8090)
    ap.add_argument("--bind", default="127.0.0.1", help="0.0.0.0 to allow phones on your Wi-Fi")
    ap.add_argument("-v", "--verbose", action="store_true", help="log every request")
    a = ap.parse_args(argv)

    root = Path(a.root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    Handler.hls_root = root
    srv = ThreadingHTTPServer((a.bind, a.port), Handler)
    srv.daemon_threads = True
    srv.verbose = a.verbose
    print(f"local origin: http://{a.bind}:{a.port}/  (serving {root})", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    os.environ.setdefault("PYTHONUNBUFFERED", "1")
    sys.exit(main())
