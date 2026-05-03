#!/usr/bin/env python3
"""
Quick Status - Get quick status of paper trading without full runner.
"""

import json
import subprocess
import sys
import os
from datetime import datetime

def main():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    manager_script = os.path.join(script_dir, 'paper_trading_manager.py')
    python = os.path.join(script_dir, '.venv', 'bin', 'python3')

    cmd = sys.argv[1] if len(sys.argv) > 1 else 'status'

    try:
        result = subprocess.run([python, manager_script, cmd],
                              capture_output=True, text=True, timeout=10)

        if result.returncode == 0 and result.stdout:
            data = json.loads(result.stdout)

            if cmd == 'status':
                print(f"\n{'='*80}")
                print(f"PAPER TRADING STATUS - {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}")
                print(f"{'='*80}\n")

                algos = data.get('algos', [])
                if algos:
                    print(f"ALGORITHMS ({len(algos)}):")
                    print("-" * 80)
                    for algo in algos:
                        print(f"{algo['name']:35} | {algo['capital']:>10} | Profit: {algo['profit']:>10} ({algo['profit_pct']:>8}) | Active: {algo['active_positions']}")
                else:
                    print("No algorithms initialized. Run: ./paper_trading_manager.py init")

                active = data.get('active_trades', [])
                if active:
                    print(f"\nACTIVE TRADES ({len(active)}):")
                    print("-" * 80)
                    for t in active:
                        print(f"{t['algo']:35} | {t['signal'].upper():4} @ {t['entry_price']:>10} | {t['reason']}")

                recent = data.get('recent_completed', [])
                if recent:
                    print(f"\nRECENT COMPLETED ({len(recent)}):")
                    print("-" * 80)
                    for t in recent:
                        pnl_str = t.get('net_pnl', t.get('pnl', '$0.00'))
                        pnl_pct_str = t.get('pnl_pct', '0.00%')
                        emoji = "📈" if float(pnl_str.replace('$','')) > 0 else "📉"
                        print(f"{emoji} {t['algo']:35} | {pnl_str:>10} ({pnl_pct_str:>8})")

            elif cmd == 'report':
                print(json.dumps(data, indent=2))

        else:
            print(f"Error: {result.stderr}")

    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    main()
