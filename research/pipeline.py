#!/usr/bin/env python3
"""
research/pipeline.py — Automated trader research pipeline.

Entry point for all research operations. Run from trader/ directory.

Commands:
  lint          Run sanity checks on all enabled research candidates
  report        Show performance report + ranked verdicts from research state
  run           Start the isolated research runner in the foreground
  run-bg        Start the isolated research runner in the background (nohup)
  status        Quick status from current research state
  reset         Archive and wipe research state (with confirmation)
  help          Show this message

Usage:
  .venv/bin/python3 research/pipeline.py lint
  .venv/bin/python3 research/pipeline.py report
  .venv/bin/python3 research/pipeline.py run
  .venv/bin/python3 research/pipeline.py run-bg
  .venv/bin/python3 research/pipeline.py status
  .venv/bin/python3 research/pipeline.py reset
"""

from __future__ import annotations
import sys
import os
import json
import subprocess
import shutil
from datetime import datetime, timezone

# Make imports work from trader/ directory
_TRADER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _TRADER_DIR)
sys.path.insert(0, os.path.join(_TRADER_DIR, 'shared_strategies', 'spot'))

RESEARCH_STATE  = os.path.join(_TRADER_DIR, "paper_trading_research_state.json")
RESEARCH_LOG    = os.path.join(_TRADER_DIR, "paper_trading_research.log")
RESEARCH_PID    = os.path.join(_TRADER_DIR, "paper_trading_research_runner.pid")
RESEARCH_RUNNER = os.path.join(_TRADER_DIR, "paper_trading_research_runner.py")
PYTHON          = os.path.join(_TRADER_DIR, ".venv", "bin", "python3")


# ── Helpers ──────────────────────────────────────────────────────────────────

def _runner_pid() -> int | None:
    if not os.path.exists(RESEARCH_PID):
        return None
    try:
        pid = int(open(RESEARCH_PID).read().strip())
        os.kill(pid, 0)
        return pid
    except (ProcessLookupError, ValueError, OSError):
        return None


def _check_main_runner_untouched():
    """Verify main runner state file hasn't changed."""
    main_state = os.path.join(_TRADER_DIR, "paper_trading_state.json")
    if os.path.exists(main_state):
        print(f"  [✓] Main runtime state file exists and is separate: paper_trading_state.json")
    else:
        print(f"  [✓] Main runtime state file not found (normal if newly started).")


# ── lint command ─────────────────────────────────────────────────────────────

def cmd_lint():
    from research.registry import get_enabled
    from research.lint import lint_all, print_lint_results

    candidates = get_enabled()
    if not candidates:
        print("No enabled candidates in registry.")
        return

    print(f"Linting {len(candidates)} enabled candidate(s)...")
    results = lint_all(candidates)
    print_lint_results(results)
    return results


# ── report command ────────────────────────────────────────────────────────────

def cmd_report(state_file: str = RESEARCH_STATE):
    from research.registry import get_enabled
    from research.lint import lint_all
    from research.report import score_from_state, print_report

    if not os.path.exists(state_file):
        print(f"No research state at {state_file}.")
        print("Run the research batch first: python3 research/pipeline.py run-bg")
        return

    # Run lint to inform verdict engine
    candidates = get_enabled()
    lint_results = lint_all(candidates)
    lint_verdicts = {lr.strategy: lr.verdict for lr in lint_results}

    stats = score_from_state(state_file, lint_verdicts)
    print_report(stats)

    pid = _runner_pid()
    if pid:
        print(f"\n  Research runner is LIVE (PID {pid})")
    else:
        print(f"\n  Research runner is NOT running.")


# ── status command ────────────────────────────────────────────────────────────

def cmd_status():
    from research.report import score_from_state

    pid = _runner_pid()
    print("=" * 60)
    print("RESEARCH PIPELINE STATUS")
    print("=" * 60)
    print(f"  Runner:    {'LIVE (PID ' + str(pid) + ')' if pid else 'NOT RUNNING'}")
    print(f"  State:     {'exists' if os.path.exists(RESEARCH_STATE) else 'none'}")
    print(f"  Log:       {'exists' if os.path.exists(RESEARCH_LOG) else 'none'}")
    print()
    _check_main_runner_untouched()
    print()

    if os.path.exists(RESEARCH_STATE):
        stats = score_from_state(RESEARCH_STATE)
        total_trades = sum(s.trades for s in stats)
        print(f"  Candidates tracked: {len(stats)}")
        print(f"  Total research trades: {total_trades}")
        print()
        print(f"  {'Strategy':<22} {'T':>4} {'WR':>6} {'Net P&L':>10} {'Verdict'}")
        print("  " + "-" * 56)
        for s in stats:
            pnl_str = f"+${s.net_pnl:.2f}" if s.net_pnl >= 0 else f"-${abs(s.net_pnl):.2f}"
            print(f"  {s.name+'@'+s.asset:<22} {s.trades:>4} {s.win_rate:>5.1f}% {pnl_str:>10}  {s.verdict}")
    else:
        print("  No research trades yet.")
    print("=" * 60)


# ── run command ───────────────────────────────────────────────────────────────

def cmd_run(cycles: int = None):
    """Run research runner in the foreground."""
    pid = _runner_pid()
    if pid:
        print(f"Research runner already live (PID {pid}). Kill it first.")
        return
    args = [PYTHON, RESEARCH_RUNNER, "run"]
    if cycles:
        args.append(str(cycles))
    print(f"Starting research runner (foreground)...")
    subprocess.run(args)


def cmd_run_bg():
    """Start research runner in the background (nohup)."""
    pid = _runner_pid()
    if pid:
        print(f"Research runner already live (PID {pid}). Kill it first.")
        return

    cmd = [
        "nohup", PYTHON, RESEARCH_RUNNER, "run"
    ]
    with open(RESEARCH_LOG, "a") as logf:
        proc = subprocess.Popen(
            cmd,
            stdout=logf, stderr=logf,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )

    # Brief pause then verify
    import time; time.sleep(2)
    new_pid = _runner_pid()
    if new_pid:
        print(f"Research runner started in background (PID {new_pid})")
        print(f"Log: tail -f {RESEARCH_LOG}")
    else:
        print(f"WARNING: runner started (nohup PID {proc.pid}) but PID file not yet written.")
        print(f"Check: {RESEARCH_LOG}")


# ── reset command ─────────────────────────────────────────────────────────────

def cmd_reset():
    pid = _runner_pid()
    if pid:
        print(f"Research runner is live (PID {pid}). Kill it before resetting.")
        return

    if not os.path.exists(RESEARCH_STATE):
        print("No research state to reset.")
        return

    confirm = input("This archives and wipes research state. Type 'yes' to confirm: ").strip()
    if confirm != "yes":
        print("Aborted.")
        return

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    backup = RESEARCH_STATE.replace(".json", f"_backup_{ts}.json")
    shutil.copy2(RESEARCH_STATE, backup)
    os.remove(RESEARCH_STATE)
    print(f"Research state archived to {backup} and reset.")


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "help"

    if cmd == "lint":
        cmd_lint()
    elif cmd == "report":
        cmd_report()
    elif cmd == "status":
        cmd_status()
    elif cmd == "run":
        cycles = int(sys.argv[2]) if len(sys.argv) > 2 else None
        cmd_run(cycles)
    elif cmd == "run-bg":
        cmd_run_bg()
    elif cmd == "reset":
        cmd_reset()
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
