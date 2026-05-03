#!/usr/bin/env python3
"""
Monitor paper trading - shows live updates
"""
import json
import time
import os
from datetime import datetime

state_file = "paper_trading_state.json"

print("=" * 80)
print("PAPER TRADING LIVE MONITOR")
print("=" * 80)
print()

while True:
    try:
        if os.path.exists(state_file):
            with open(state_file, 'r') as f:
                state = json.load(f)

            print(f"\r[{datetime.utcnow().strftime('%H:%M:%S')}] ", end="")
            print(f"Algos: {len(state.get('algos', {}))} | ", end="")
            print(f"Active: {len(state.get('active_trades', []))} | ", end="")
            print(f"Completed: {len(state.get('completed_trades', []))}", end="")

            # Show any active trades
            active = state.get('active_trades', [])
            if active:
                print(f" | TRADES: ", end="")
                for t in active[:3]:
                    print(f"{t.get('algo')}-{t.get('asset')} ", end="")
        else:
            print(f"\r[{datetime.utcnow().strftime('%H:%M:%S')}] Waiting for state file...", end="")

        time.sleep(2)

    except KeyboardInterrupt:
        print("\n\nMonitoring stopped.")
        break
    except Exception as e:
        print(f"\rError: {e}", end="")
        time.sleep(2)
