#!/usr/bin/env bash
# Start a streaming session (create server from snapshot, attach reserved IP,
# boot, health check). Safe to re-run: it resumes. See docs/RUNBOOK.md.
#   ./start_session.sh [--session 20261111-1900] [--destroy-on-failure]
set -euo pipefail
cd "$(dirname "$0")"
for c in python3 python; do
  if "$c" -c 'import sys; sys.exit(sys.version_info < (3, 9))' 2>/dev/null; then PY=$c; break; fi
done
: "${PY:?Python 3.9+ not found}"
exec "$PY" control/livestack.py start "$@"
