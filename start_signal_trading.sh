#!/bin/bash
# Start the signal trading system (BTC, ETH, Gold).
# Runs as a background process alongside paper_trading_runner.py.
# PID lockfile prevents duplicate instances.

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON="$SCRIPT_DIR/.venv/bin/python3"
LOG="$SCRIPT_DIR/signal_trading/signal_trading.log"
PID_FILE="$SCRIPT_DIR/signal_trading/signal_trading.pid"

# Check if already running
if [ -f "$PID_FILE" ]; then
    OLD_PID=$(cat "$PID_FILE")
    if kill -0 "$OLD_PID" 2>/dev/null; then
        echo "Already running (PID $OLD_PID). Kill it first:"
        echo "  kill $OLD_PID && rm $PID_FILE"
        exit 1
    else
        echo "Stale PID file found (PID $OLD_PID was dead). Cleaning up."
        rm -f "$PID_FILE"
    fi
fi

echo "Starting signal trading (BTC + ETH + Gold)..."
cd "$SCRIPT_DIR"
nohup "$PYTHON" -m signal_trading.runner run >> "$LOG" 2>&1 &
NEW_PID=$!
echo "Started PID=$NEW_PID"
echo "Log: tail -f $LOG"
echo "Status: $PYTHON -m signal_trading.runner status"
