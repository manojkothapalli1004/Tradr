#!/usr/bin/env bash
set -euo pipefail

TRADER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
START_SCRIPT="$TRADER_DIR/start_options_bot.sh"
STATUS_SCRIPT="$TRADER_DIR/start_options_bot.sh"
PID_FILE="$TRADER_DIR/options_bot/options_bot.pid"
VENV_PY="$TRADER_DIR/.venv/bin/python3"
CRON_LOG="$TRADER_DIR/logs/cron_options_open.log"

log() {
    printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S %Z')" "$*" >> "$CRON_LOG"
}

if [[ ! -f "$VENV_PY" ]]; then
    log "ERROR: missing venv python at $VENV_PY"
    exit 1
fi

if [[ ! -x "$START_SCRIPT" ]]; then
    log "ERROR: missing executable start script at $START_SCRIPT"
    exit 1
fi

if [[ -f "$PID_FILE" ]]; then
    OLD_PID=$(cat "$PID_FILE" 2>/dev/null || echo "")
    if [[ -n "$OLD_PID" ]] && kill -0 "$OLD_PID" 2>/dev/null; then
        log "options_bot already running (PID $OLD_PID); skipping start"
        "$STATUS_SCRIPT" status >> "$CRON_LOG" 2>&1 || log "WARN: status check failed while process was already running"
        exit 0
    fi
    log "stale PID file detected for options_bot (PID ${OLD_PID:-unknown}); removing"
    rm -f "$PID_FILE"
fi

log "starting options_bot via $START_SCRIPT"
nohup "$START_SCRIPT" >> "$CRON_LOG" 2>&1 &
LAUNCH_PID=$!
log "start command launched (shell PID $LAUNCH_PID)"

sleep 5

if [[ -f "$PID_FILE" ]]; then
    NEW_PID=$(cat "$PID_FILE" 2>/dev/null || echo "")
    if [[ -n "$NEW_PID" ]] && kill -0 "$NEW_PID" 2>/dev/null; then
        log "options_bot confirmed running (PID $NEW_PID)"
        "$STATUS_SCRIPT" status >> "$CRON_LOG" 2>&1 || log "WARN: post-start status check failed"
        exit 0
    fi
fi

log "ERROR: options_bot did not produce a live PID after startup"
exit 1
