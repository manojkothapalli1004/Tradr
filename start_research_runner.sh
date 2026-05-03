#!/bin/bash
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"
# If a previous launchd-managed run died mid-flight, the PID file lock can be
# stale. The runner self-heals (ProcessLookupError → proceed) so this is
# defensive only.
exec "$SCRIPT_DIR/.venv/bin/python3" "$SCRIPT_DIR/paper_trading_research_runner.py" run
