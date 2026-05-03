#!/usr/bin/env python3
import json

# Load state
with open('paper_trading_state.json', 'r') as f:
    state = json.load(f)

print('='*80)
print('ACTIVE TRADES PROFIT/LOSS ANALYSIS')
print('='*80)
print()

# Approximate current prices
btc_current = 74000
eth_current = 2300

total_pnl = 0

for trade in state['active_trades']:
    trade_id = trade['id']
    algo = trade['algo']
    asset = trade['asset']
    signal = trade['signal']
    entry_price = trade['entry_price']
    size = trade['size']

    # Get current price
    if asset == 'BTC':
        current_price = btc_current
    else:
        current_price = eth_current

    # Calculate unrealized P&L
    if signal == 'buy':
        pnl_pct = ((current_price - entry_price) / entry_price) * 100
    else:
        pnl_pct = ((entry_price - current_price) / entry_price) * 100

    pnl_usd = (pnl_pct / 100) * size
    total_pnl += pnl_usd

    status = 'PROFIT' if pnl_usd > 0 else 'LOSS'
    emoji = '📈' if pnl_usd > 0 else '📉'

    print(f'{emoji} {trade_id}')
    print(f'   Algo: {algo} ({asset})')
    print(f'   Signal: {signal.upper()}')
    print(f'   Entry: ${entry_price:.2f}')
    print(f'   Current: ~${current_price:.2f}')
    print(f'   P&L: ${pnl_usd:.2f} ({pnl_pct:+.2f}%) - {status}')
    print()

print('='*80)
print(f'TOTAL UNREALIZED P&L: ${total_pnl:.2f}')
print('='*80)
