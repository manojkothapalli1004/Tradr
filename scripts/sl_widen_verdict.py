"""One-shot verdict applier for sl-widen-2026-04-26.

Reads paper_trading_research_state.json, counts orb@ETH trades since the
2026-04-27 scope-fix cutoff, and — if >= 30 trades — applies the verdict
to research_manager/experiment_journal.json per the success/neutral/failure
thresholds in the journal entry.

Idempotent: if status is already not "running", does nothing.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

TRADER_DIR = Path(__file__).resolve().parents[1]
STATE_FILE = TRADER_DIR / "paper_trading_research_state.json"
JOURNAL_FILE = TRADER_DIR / "research_manager" / "experiment_journal.json"
LOG_DIR = TRADER_DIR / "logs"

EXPERIMENT_ID = "sl-widen-2026-04-26"
SAMPLE_TARGET = 30
CUTOFF = datetime(2026, 4, 27, 0, 0, tzinfo=timezone.utc)

THRESHOLDS = {
    "SUCCESS": ("net >= +$1.0", lambda net: net >= 1.0),
    "FAILURE": ("net <= -$1.0", lambda net: net <= -1.0),
    "NEUTRAL": ("|net| <= $0.5", lambda net: abs(net) <= 0.5),
}


def macos_notify(title: str, body: str) -> None:
    try:
        subprocess.run(
            ["osascript", "-e", f'display notification "{body}" with title "{title}"'],
            check=False, timeout=5,
        )
    except Exception:
        pass


def main() -> int:
    sys.path.insert(0, str(TRADER_DIR))
    from research_manager.experiment_journal import load_journal, save_journal

    LOG_DIR.mkdir(exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    log_path = LOG_DIR / f"sl_widen_verdict_{today}.log"

    def log(msg: str) -> None:
        line = f"{datetime.now().isoformat()} {msg}"
        print(line)
        with open(log_path, "a") as f:
            f.write(line + "\n")

    log(f"=== sl_widen_verdict starting (experiment={EXPERIMENT_ID}) ===")

    journal = load_journal(str(JOURNAL_FILE))
    entry = journal.entries.get(EXPERIMENT_ID)
    if entry is None:
        log(f"ABORT: experiment {EXPERIMENT_ID} not in journal")
        macos_notify("sl-widen verdict", f"ABORT: {EXPERIMENT_ID} not in journal")
        return 1
    if entry.status != "running":
        log(f"NO-OP: status={entry.status} verdict={entry.verdict!r} (already closed)")
        macos_notify("sl-widen verdict", f"already {entry.status}/{entry.verdict}")
        return 0

    if not STATE_FILE.exists():
        log(f"ABORT: state file missing: {STATE_FILE}")
        return 2
    state = json.load(open(STATE_FILE))
    trades = state.get("completed_trades", [])

    orb_eth = [t for t in trades if t.get("algo") == "orb" and t.get("asset") == "ETH"]
    post = []
    for t in orb_eth:
        et = t.get("exit_time") or t.get("entry_time")
        if not et:
            continue
        try:
            dt = datetime.fromisoformat(et.replace("Z", "+00:00"))
        except ValueError:
            continue
        if dt >= CUTOFF:
            post.append(t)

    sample = len(post)
    net = sum(float(t.get("net_pnl") or 0) for t in post)
    wins = sum(1 for t in post if float(t.get("net_pnl") or 0) > 0)
    wr = (wins / sample * 100) if sample else 0.0

    log(f"orb@ETH post-cutoff: sample={sample} net=${net:+.4f} wr={wr:.1f}%")

    if sample < SAMPLE_TARGET:
        log(f"INSUFFICIENT SAMPLE ({sample}/{SAMPLE_TARGET}); leaving status=running. "
            "Re-arm verdict check to fire later.")
        macos_notify("sl-widen verdict",
                     f"sample {sample}/{SAMPLE_TARGET} — re-arm needed")
        return 3

    verdict = "INCONCLUSIVE"
    why = "outside both ±$0.5 and ±$1.0 bands"
    for v, (desc, pred) in THRESHOLDS.items():
        if pred(net):
            verdict = v
            why = desc
            break

    note = (
        f"AUTO-VERDICT {datetime.now().isoformat()}: "
        f"sample={sample}, net=${net:+.4f}, wr={wr:.1f}% → {verdict} ({why})."
    )
    log(note)

    journal.update_status(
        EXPERIMENT_ID,
        status="completed",
        end_timestamp=datetime.now(timezone.utc).isoformat(),
        verdict=verdict,
        sample_size=sample,
        note=note,
    )
    save_journal(journal, str(JOURNAL_FILE))
    log(f"journal updated: {EXPERIMENT_ID} → completed/{verdict}")
    macos_notify("sl-widen verdict",
                 f"{verdict} (sample={sample} net=${net:+.2f})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
