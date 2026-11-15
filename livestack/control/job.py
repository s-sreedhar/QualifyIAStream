#!/usr/bin/env python3
"""Long-running livestream operations, run as a Cloud Run Job (or locally for testing).

  python control/job.py start <session-id>
  python control/job.py end   <session-id> [save|discard]
  python control/job.py monitor <session-id>
  python control/job.py monitor-all

The backend claims a session first (status 'starting'/'ending') and launches this.
All progress goes to the database; the admin dashboard shows it.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cms_bridge  # noqa: E402
from common import log, setup_logging  # noqa: E402


def main(argv: list[str]) -> int:
    if not argv or argv[0] not in ("start", "end", "monitor", "monitor-all"):
        print(__doc__)
        return 2
    action = argv[0]
    if action == "monitor-all" and len(argv) != 1:
        print(__doc__)
        return 2
    if action != "monitor-all" and len(argv) < 2:
        print(__doc__)
        return 2
    sid = argv[1] if action != "monitor-all" else ""
    mode = argv[2] if len(argv) > 2 else "save"
    if mode not in ("save", "discard"):
        print("mode must be 'save' or 'discard'")
        return 2
    setup_logging()
    cms_bridge.prepare_environment()
    store = cms_bridge.Store.from_env()
    try:
        if action == "start":
            return cms_bridge.run_start(store, sid)
        if action == "end":
            return cms_bridge.run_end(store, sid, mode)
        if action == "monitor-all":
            cms_bridge.reconcile_stuck(store)
            cms_bridge.monitor_all(store)
            return 0
        row = store.get(sid)
        if not row:
            log.error("session %s not found", sid)
            return 2
        cms_bridge.monitor_session(store, row)
        return 0
    except SystemExit as e:
        log.error("%s", e)
        return 1
    except Exception as e:  # noqa: BLE001  (run_* already recorded what they could)
        log.exception("job failed: %s", e)
        try:
            store.update(sid, status="failed", error=cms_bridge.redact(f"{type(e).__name__}: {e}")[:1000])
            store.event(sid, "error", "job", f"Job crashed: {type(e).__name__}: {e}")
        except Exception:  # noqa: BLE001
            pass
        return 1
    finally:
        store.close()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
