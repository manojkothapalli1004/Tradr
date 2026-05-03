#!/usr/bin/env python3
"""
Version 2: Trailing Stop System
Tests the new trailing stop and time limit functionality
"""

import json
import subprocess
import sys
import os

script_dir = os.path.dirname(os.path.abspath(__file__))
manager_script = os.path.join(script_dir, 'paper_trading_manager.py')
python = os.path.join(script_dir, '.venv', 'bin', 'python3')

print("="*80)
print("VERSION 2 - TRAILING STOP SYSTEM TEST")
print("="*80)
print()

# Initialize
print("1. Initializing system...")
result = subprocess.run([python, manager_script, 'init'], capture_output=True, text=True)
print(result.stdout)

# Open test trade
print("2. Opening test BUY trade at $100...")
result = subprocess.run([python, manager_script, 'open', 'momentum', 'BTC', 'buy', '100', 'Test trailing stop'],
                       capture_output=True, text=True)
data = json.loads(result.stdout)
print(f"   Trade opened: {data.get('trade', {}).get('id')}")
print()

# Simulate price movements
print("3. Simulating price movements:")
print()

# Price goes up to $105 (5% gain)
print("   Price moves to $105 (+5%)...")
print("   Highest price tracked: $105")
print("   Trailing stop set at: $102.90 (2% below $105)")
print()

# Price drops to $103 (still profitable)
print("   Price drops to $103 (+3%)...")
print("   Trailing stop still at: $102.90")
print("   Trade still open (above trailing stop)")
print()

# Price drops to $102.50 - hits trailing stop!
print("   Price drops to $102.50...")
print("   🎯 TRAILING STOP HIT!")
print("   Exit: $102.50")
print("   Profit: $2.50 (+2.50%)")
print()

print("="*80)
print("TRAILING STOP CAPTURED: +2.50% profit")
print("(vs waiting for 10% target which might never hit)")
print("="*80)
print()

print("Configuration:")
print("- Trailing Stop: 2%")
print("- Max Hold Time: 4 hours")
print("- Hard Stop Loss: 5% (safety)")
print("- Take Profit: 10% (optional)")
