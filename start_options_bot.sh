#!/usr/bin/env bash
# start_options_bot.sh — Launch the options paper-trading bot.
#
# PAPER TRADING ONLY. No real orders are placed.
# Operates independently of the existing spot bot.
# Does not touch: signal_trading/, paper_trading_*.py, shared_scripts/,
#                 shared_strategies/, platforms/, scheduler/*.go
#
# Usage:
#   ./start_options_bot.sh              # run indefinitely
#   ./start_options_bot.sh status       # print current state and exit
#   ./start_options_bot.sh N            # run N signal cycles then exit (smoke test)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_PY="$SCRIPT_DIR/.venv/bin/python3"
PID_FILE="$SCRIPT_DIR/options_bot/options_bot.pid"

# ── Preflight checks ──────────────────────────────────────────────────────────────

if [[ ! -f "$VENV_PY" ]]; then
    echo "ERROR: .venv not found. Run 'uv sync' first." >&2
    exit 1
fi

if ! "$VENV_PY" -c "import yfinance" 2>/dev/null; then
    echo "ERROR: yfinance is not installed. Run: uv add yfinance" >&2
    exit 1
fi

# ── Status subcommand ─────────────────────────────────────────────────────────────

if [[ "${1:-}" == "status" ]]; then
    cd "$SCRIPT_DIR"
    PYTHONPATH="$SCRIPT_DIR" "$VENV_PY" -m options_bot.runner status
    exit 0
fi

# ── PID guard ─────────────────────────────────────────────────────────────────────

if [[ -f "$PID_FILE" ]]; then
    OLD_PID=$(cat "$PID_FILE" 2>/dev/null || echo "")
    if [[ -n "$OLD_PID" ]] && kill -0 "$OLD_PID" 2>/dev/null; then
        echo "ERROR: options_bot already running (PID $OLD_PID)." >&2
        echo "Stop it first, or delete $PID_FILE if the process is dead." >&2
        exit 1
    else
        echo "Stale PID file (PID $OLD_PID). Removing." >&2
        rm -f "$PID_FILE"
    fi
fi

# ── Launch ────────────────────────────────────────────────────────────────────────

N_CYCLES="${1:-}"

echo "[options_bot] Starting paper-trading bot..."
echo "[options_bot] Data: yfinance free tier (~15-min delayed). Fills are theoretical."
echo "[options_bot] This bot is PAPER ONLY. No real orders will be placed."
echo ""

cd "$SCRIPT_DIR"

if [[ -n "$N_CYCLES" ]]; then
    exec env PYTHONPATH="$SCRIPT_DIR" "$VENV_PY" -m options_bot.runner run "$N_CYCLES"
else
    exec env PYTHONPATH="$SCRIPT_DIR" "$VENV_PY" -m options_bot.runner run
fi
