#!/usr/bin/env bash
# Stop a session: verify the replay is in R2, then destroy the server.
# Keeps the reserved IP and snapshot. See docs/RUNBOOK.md.
#   ./stop_session.sh                       (uses the upload manifest of this session)
#   ./stop_session.sh --replay-key K --replay-file F
set -euo pipefail
cd "$(dirname "$0")"
for c in python3 python; do
  if "$c" -c 'import sys; sys.exit(sys.version_info < (3, 9))' 2>/dev/null; then PY=$c; break; fi
done
: "${PY:?Python 3.9+ not found}"
exec "$PY" control/livestack.py stop "$@"
