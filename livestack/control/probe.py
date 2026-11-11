#!/usr/bin/env python3
"""HLS freshness probe - watches a live master playlist like a viewer would.

Every --interval seconds it fetches master.m3u8 and every variant playlist.
If a variant's newest segment has not advanced for --stale-after seconds, or a
variant is missing/unreachable, it raises an alert (ALERT_WEBHOOK_URL) once,
re-alerts every --renotify seconds while the problem lasts, and sends a
RESOLVED message when it recovers.

Run it from OUTSIDE the server (your PC, another VPS) against the CDN URL so it
sees what viewers see:

    python control/probe.py https://live.example.com/hls/live/test/master.m3u8

One-shot check (exit code 0 = healthy), used by the local test:

    python control/probe.py http://127.0.0.1:8090/hls/live/test/master.m3u8 --once   (local\run_local.ps1)
"""
from __future__ import annotations

import argparse
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import alert, load_env, log, setup_logging  # noqa: E402

UA = "livestack-probe/1.0"


# --------------------------------------------------------------------------- parsing
def parse_master(text: str, base_url: str) -> list[tuple[str, str]]:
    """Return [(stream-inf attributes, absolute variant URL), ...]."""
    if not text.lstrip().startswith("#EXTM3U"):
        raise ValueError("not an M3U8 playlist")
    variants, pending = [], None
    for line in (ln.strip() for ln in text.splitlines()):
        if line.startswith("#EXT-X-STREAM-INF"):
            pending = line.split(":", 1)[1] if ":" in line else ""
        elif line and not line.startswith("#") and pending is not None:
            variants.append((pending, urllib.parse.urljoin(base_url, line)))
            pending = None
    return variants


@dataclass
class MediaPlaylist:
    media_sequence: int = 0
    target_duration: float | None = None
    endlist: bool = False
    segments: list[str] = field(default_factory=list)

    @property
    def last_index(self) -> int:
        return self.media_sequence + len(self.segments) - 1


def parse_media(text: str) -> MediaPlaylist:
    if not text.lstrip().startswith("#EXTM3U"):
        raise ValueError("not an M3U8 playlist")
    pl = MediaPlaylist()
    for line in (ln.strip() for ln in text.splitlines()):
        if line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            pl.media_sequence = int(line.split(":", 1)[1])
        elif line.startswith("#EXT-X-TARGETDURATION:"):
            pl.target_duration = float(line.split(":", 1)[1])
        elif line.startswith("#EXT-X-ENDLIST"):
            pl.endlist = True
        elif line and not line.startswith("#"):
            pl.segments.append(line)
    return pl


# --------------------------------------------------------------------------- state
class StaleTracker:
    """Remembers when each variant's newest segment last advanced."""

    def __init__(self, stale_after: float):
        self.stale_after = stale_after
        self._last: dict[str, tuple[int, float]] = {}

    def update(self, key: str, last_index: int, now: float) -> float:
        """Record an observation and return seconds since the playlist last advanced."""
        prev = self._last.get(key)
        if prev is None or last_index > prev[0]:
            self._last[key] = (last_index, now)
            return 0.0
        return now - prev[1]

    def is_stale(self, age: float) -> bool:
        return age > self.stale_after


class Alerter:
    """Alert on a new problem, re-alert every `renotify` s, and announce recovery."""

    def __init__(self, renotify: float = 300, send: Callable[[str, str], object] = alert,
                 clock: Callable[[], float] = time.monotonic):
        self.renotify, self.send, self.clock = renotify, send, clock
        self.active: dict[str, float] = {}

    def fire(self, key: str, message: str, level: str = "error") -> None:
        now = self.clock()
        last = self.active.get(key)
        if last is None or now - last >= self.renotify:
            self.active[key] = now
            self.send(message, level)

    def resolve(self, key: str, message: str) -> None:
        if self.active.pop(key, None) is not None:
            self.send(f"RESOLVED: {message}", "info")


# --------------------------------------------------------------------------- network
def fetch(url: str, timeout: float = 10, method: str = "GET") -> tuple[int, bytes]:
    req = urllib.request.Request(url, method=method, headers={"User-Agent": UA, "Cache-Control": "no-cache"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, (r.read() if method == "GET" else b"")


def fetch_text(url: str, timeout: float = 10) -> str:
    return fetch(url, timeout)[1].decode("utf-8", "replace")


# --------------------------------------------------------------------------- checks
def check_round(url: str, expect: int, tracker: StaleTracker, alerter: Alerter,
                check_segments: bool, timeout: float) -> list[str]:
    """One probe round. Returns a list of problems (empty = healthy)."""
    problems: list[str] = []
    try:
        variants = parse_master(fetch_text(url, timeout), url)
    except Exception as e:
        msg = f"master playlist unreachable: {url}: {e}"
        alerter.fire("master", msg)
        return [msg]
    alerter.resolve("master", f"master playlist reachable: {url}")

    if len(variants) != expect:
        msg = f"expected {expect} variants, master lists {len(variants)}"
        alerter.fire("variants", msg)
        problems.append(msg)
    else:
        alerter.resolve("variants", f"{expect} variants listed")

    now = time.monotonic()
    for _, vurl in variants:
        name = vurl.rstrip("/").split("/")[-2] if "/" in vurl else vurl
        key = f"variant:{name}"
        try:
            pl = parse_media(fetch_text(vurl, timeout))
            if not pl.segments:
                raise ValueError("playlist has no segments")
        except Exception as e:
            msg = f"variant {name} unreachable/invalid: {e}"
            alerter.fire(key, msg)
            problems.append(msg)
            continue
        alerter.resolve(key, f"variant {name} reachable")

        age = tracker.update(name, pl.last_index, now)
        if pl.endlist:
            msg = f"variant {name} has #EXT-X-ENDLIST (stream ended)"
            alerter.fire(f"stale:{name}", msg, "warning")
            problems.append(msg)
        elif tracker.is_stale(age):
            msg = f"variant {name} stale: newest segment unchanged for {age:.0f}s (seq {pl.last_index})"
            alerter.fire(f"stale:{name}", msg)
            problems.append(msg)
        else:
            alerter.resolve(f"stale:{name}", f"variant {name} advancing again (seq {pl.last_index})")

        if check_segments:
            seg_url = urllib.parse.urljoin(vurl, pl.segments[-1])
            try:
                status, _ = fetch(seg_url, timeout, method="HEAD")
                if status != 200:
                    raise ValueError(f"HTTP {status}")
                alerter.resolve(f"segment:{name}", f"segments of {name} downloadable")
            except Exception as e:
                msg = f"segment of {name} not downloadable ({seg_url}): {e}"
                alerter.fire(f"segment:{name}", msg)
                problems.append(msg)
        log.debug("%s seq=%d age=%.1fs", name, pl.last_index, age)
    return problems


def run_once(url: str, expect: int, timeout: float, advance_check: bool) -> int:
    """Single health check; with advance_check, also confirms playlists move forward."""
    collected: list[str] = []
    alerter = Alerter(send=lambda m, lvl: None)  # one-shot: report via exit code, not webhook
    tracker = StaleTracker(stale_after=1e9)
    problems = check_round(url, expect, tracker, alerter, True, timeout)
    collected += problems
    if not problems and advance_check:
        first = dict(tracker._last)
        wait = 6.0
        try:
            v = parse_master(fetch_text(url, timeout), url)
            td = parse_media(fetch_text(v[0][1], timeout)).target_duration
            wait = (td or 4) * 1.5 + 1
        except Exception:
            pass
        log.info("waiting %.0fs to confirm playlists advance...", wait)
        time.sleep(wait)
        check_round(url, expect, tracker, alerter, False, timeout)
        for name, (idx, _) in first.items():
            if tracker._last.get(name, (idx, 0))[0] <= idx:
                collected.append(f"variant {name} did not advance in {wait:.0f}s")
    for p in collected:
        log.error("FAIL %s", p)
    if not collected:
        log.info("OK %s - %d variants, all advancing and downloadable", url, expect)
    return 1 if collected else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("url", help="master.m3u8 URL")
    ap.add_argument("--interval", type=float, default=10, help="seconds between rounds (default 10)")
    ap.add_argument("--stale-after", type=float, default=20, help="alert if no new segment for N s (default 20)")
    ap.add_argument("--expect-variants", type=int, default=3)
    ap.add_argument("--renotify", type=float, default=300, help="repeat an active alert every N s")
    ap.add_argument("--segment-check-every", type=int, default=3, help="HEAD the newest segment every N rounds")
    ap.add_argument("--timeout", type=float, default=10)
    ap.add_argument("--duration", type=float, default=0, help="stop after N s (0 = run forever)")
    ap.add_argument("--once", action="store_true", help="single check, exit 0 if healthy")
    ap.add_argument("--no-advance-check", action="store_true", help="with --once: skip the advancing check")
    ap.add_argument("--env-file")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)
    setup_logging(a.verbose)
    load_env(a.env_file)

    if a.once:
        return run_once(a.url, a.expect_variants, a.timeout, not a.no_advance_check)

    tracker, alerter = StaleTracker(a.stale_after), Alerter(a.renotify)
    start, rnd = time.monotonic(), 0
    log.info("probing %s every %.0fs (stale after %.0fs)", a.url, a.interval, a.stale_after)
    try:
        while not a.duration or time.monotonic() - start < a.duration:
            t0 = time.monotonic()
            problems = check_round(a.url, a.expect_variants, tracker, alerter,
                                   rnd % max(a.segment_check_every, 1) == 0, a.timeout)
            log.info("round %d: %s", rnd, "OK" if not problems else "; ".join(problems))
            rnd += 1
            time.sleep(max(0.0, a.interval - (time.monotonic() - t0)))
    except KeyboardInterrupt:
        log.info("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
