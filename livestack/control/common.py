"""Shared helpers for the livestack control tools (stdlib only).

- load_env():  reads KEY=VALUE lines from livestack/.env (never overrides real env vars)
- require():   fails fast with a clear message when a setting is missing
- retry():     exponential backoff for transient failures
- alert():     posts to ALERT_WEBHOOK_URL (Discord, Slack or ntfy); never raises
"""
from __future__ import annotations

import json
import logging
import os
import time
import urllib.request
from pathlib import Path
from typing import Callable, Iterable, TypeVar

ROOT = Path(__file__).resolve().parent.parent
log = logging.getLogger("livestack")
T = TypeVar("T")


class TransientError(RuntimeError):
    """A failure that is worth retrying (HTTP 429/5xx, timeouts, network)."""


def setup_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def parse_env_text(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        out[key] = value
    return out


def load_env(path: str | os.PathLike | None = None) -> None:
    """Load .env into os.environ. Real environment variables win."""
    p = Path(path) if path else ROOT / ".env"
    if not p.exists():
        if path:
            raise SystemExit(f"env file not found: {p}")
        return
    for key, value in parse_env_text(p.read_text(encoding="utf-8")).items():
        if value != "":
            os.environ.setdefault(key, value)


def require(*names: str) -> list[str]:
    missing = [n for n in names if not os.environ.get(n)]
    if missing:
        raise SystemExit(
            "Missing required settings: " + ", ".join(missing)
            + " (set them in livestack/.env or the environment; see .env.example)"
        )
    return [os.environ[n] for n in names]


def retry(
    fn: Callable[[], T],
    attempts: int = 5,
    base_delay: float = 2.0,
    max_delay: float = 60.0,
    retry_on: Iterable[type[BaseException]] = (Exception,),
    what: str = "operation",
) -> T:
    retry_on = tuple(retry_on)
    for i in range(1, attempts + 1):
        try:
            return fn()
        except retry_on as e:  # type: ignore[misc]
            if i == attempts:
                raise
            delay = min(base_delay * 2 ** (i - 1), max_delay)
            log.warning("%s failed (attempt %d/%d): %s - retrying in %.1fs", what, i, attempts, e, delay)
            time.sleep(delay)
    raise AssertionError("unreachable")


def wait_until(pred: Callable[[], T], timeout: float, interval: float, what: str) -> T:
    deadline = time.monotonic() + timeout
    while True:
        result = pred()
        if result:
            return result
        if time.monotonic() >= deadline:
            raise TimeoutError(f"timed out after {timeout:.0f}s waiting for {what}")
        time.sleep(interval)


def alert(message: str, level: str = "warning") -> bool:
    """Log and send an alert. Returns True if a webhook accepted it. Never raises."""
    log.log(logging.ERROR if level in ("error", "critical") else logging.WARNING, "ALERT: %s", message)
    url = os.environ.get("ALERT_WEBHOOK_URL")
    if not url:
        return False
    text = f"[livestack][{level}] {message}"
    try:
        if "ntfy" in url:
            req = urllib.request.Request(url, data=text.encode(), method="POST")
        else:  # Discord uses "content", Slack uses "text"; each ignores the other.
            req = urllib.request.Request(
                url,
                data=json.dumps({"content": text, "text": text}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
        urllib.request.urlopen(req, timeout=10).close()
        return True
    except Exception as e:  # alerting must never break the caller
        log.error("alert delivery failed: %s", e)
        return False
