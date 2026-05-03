"""
Options-bot analysis — pure functions over raw options state.

Two schemas are supported transparently:

  * Legacy `options_bot/state.json` — keys: completed_trades, algos, portfolio
    (cumulative_pnl_usd / peak_equity_usd / current_drawdown_pct /
    kill_switch_active baked in at top level).

  * Live `options_trading_state.json` — keys: completed, strategies, positions,
    portfolio (initial_capital / cash / peak_value), risk (kill_switch_active).
    `_normalize_state` rewrites this into the legacy shape so the rest of
    the analyzer is schema-agnostic.

No I/O. No side effects.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Dict, Optional


# ── Metrics dataclasses ─────────────────────────────────────────────


@dataclass
class StrategyMetrics:
    """Per-strategy (across all symbols) performance."""
    strategy: str
    trades: int = 0
    wins: int = 0
    losses: int = 0
    realized_pnl: float = 0.0
    fees: float = 0.0
    avg_hold_days: float = 0.0
    win_rate: float = 0.0


@dataclass
class SymbolMetrics:
    """Per-symbol (across all strategies) performance."""
    symbol: str
    trades: int = 0
    wins: int = 0
    losses: int = 0
    realized_pnl: float = 0.0
    fees: float = 0.0
    avg_hold_days: float = 0.0
    win_rate: float = 0.0


@dataclass
class SlotMetrics:
    """Per strategy-symbol slot performance."""
    slot: str  # "strategy-symbol"
    strategy: str
    symbol: str
    trades: int = 0
    wins: int = 0
    losses: int = 0
    realized_pnl: float = 0.0
    fees: float = 0.0
    avg_hold_days: float = 0.0
    win_rate: float = 0.0
    consecutive_losses: int = 0
    circuit_breaker_active: bool = False


@dataclass
class OptionsAnalysisResult:
    # summary
    total_trades: int = 0
    active_trades_count: int = 0
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    fees_paid: float = 0.0
    win_rate: float = 0.0
    avg_hold_days: float = 0.0

    # portfolio
    cumulative_pnl: float = 0.0
    premium_deployed: float = 0.0
    peak_equity: float = 0.0
    drawdown_pct: float = 0.0
    kill_switch_active: bool = False

    # breakdowns
    exit_reason_breakdown: Dict[str, int] = field(default_factory=dict)
    strategy_metrics: Dict[str, StrategyMetrics] = field(default_factory=dict)
    symbol_metrics: Dict[str, SymbolMetrics] = field(default_factory=dict)
    slot_metrics: Dict[str, SlotMetrics] = field(default_factory=dict)

    # rankings
    strongest_strategy: Optional[str] = None
    weakest_strategy: Optional[str] = None

    # recommendations
    sample_confidence: str = "LOW"
    continue_recommendation: str = ""
    narrow_recommendation: str = ""

    # metadata
    start_time: Optional[str] = None
    analysis_time: str = ""
    runtime_hours: float = 0.0
    total_slots: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


# ── Analysis ────────────────────────────────────────────────────────


def _normalize_state(state: dict) -> dict:
    """If `state` is in the live `options_trading_state.json` schema (has
    `completed`/`positions`/`strategies`/`risk`), rewrite it into the legacy
    `options_bot/state.json` shape (`completed_trades`/`open_trades`/`algos`/
    flattened portfolio). Returns input unchanged for legacy schema or unknowns.
    """
    is_live = "completed" in state and "completed_trades" not in state
    if not is_live:
        return state

    out = dict(state)

    # completed → completed_trades, with field aliases
    new_completed = []
    for t in state.get("completed", []) or []:
        if not isinstance(t, dict):
            continue
        nt = dict(t)
        nt["symbol"] = t.get("underlying", t.get("symbol", "unknown"))
        nt["realized_pnl_usd"] = float(t.get("net_pnl") or 0.0)
        total_fees = float(t.get("total_fees") or 0.0)
        exit_fees = float(t.get("exit_fees") or 0.0)
        nt["entry_fee_usd"] = max(0.0, total_fees - exit_fees)
        nt["exit_fee_usd"] = exit_fees
        nt["hold_days"] = float(t.get("hold_minutes") or 0.0) / 1440.0
        new_completed.append(nt)
    out["completed_trades"] = new_completed

    # strategies (slot dict) → algos
    strategies = state.get("strategies") or {}
    if isinstance(strategies, dict):
        algos = {}
        for slot_key, slot in strategies.items():
            if not isinstance(slot, dict):
                continue
            algos[slot_key] = {
                "strategy": slot.get("strategy", ""),
                "symbol": slot.get("underlying", slot.get("symbol", "")),
                "total_trades": slot.get("total_trades", 0),
                "winning_trades": slot.get("winning_trades", 0),
                "losing_trades": slot.get("losing_trades", 0),
                "total_pnl_usd": slot.get("total_pnl", 0.0),
                "consecutive_losses": slot.get("consecutive_losses", 0),
                "circuit_breaker_until": slot.get("circuit_breaker_until"),
            }
        out["algos"] = algos

    # positions (dict id→record) → open_trades list with derived current_pnl_usd
    positions = state.get("positions") or {}
    open_trades_iter = positions.values() if isinstance(positions, dict) else positions
    open_trades = []
    for p in open_trades_iter:
        if not isinstance(p, dict):
            continue
        op = dict(p)
        # short premium structures (strangle, condor): unrealized = entry credit − cost-to-close.
        # current_value reflects the cost-to-close mark price.
        credit = float(p.get("total_credit") or 0.0)
        mark = float(p.get("current_value") or 0.0)
        op.setdefault("current_pnl_usd", credit - mark)
        open_trades.append(op)
    out["open_trades"] = open_trades

    # portfolio + risk → flattened portfolio (legacy keys analyze() reads)
    portfolio = dict(state.get("portfolio") or {})
    risk = state.get("risk") or {}
    initial = float(portfolio.get("initial_capital") or 0.0)
    peak = float(portfolio.get("peak_value") or initial)
    # Equity is initial capital plus realized + unrealized PnL. `current_pnl_usd`
    # set above already encodes direction (credit − mark for shorts), so summing
    # signed PnL works for any structure mix without per-position guesswork.
    realized_pnl_total = sum(float(t.get("realized_pnl_usd") or 0.0) for t in new_completed)
    unrealized_pnl_total = sum(float(p.get("current_pnl_usd") or 0.0) for p in open_trades)
    current_equity = initial + realized_pnl_total + unrealized_pnl_total
    portfolio["cumulative_pnl_usd"] = realized_pnl_total + unrealized_pnl_total
    portfolio["peak_equity_usd"] = peak
    portfolio["total_premium_deployed_usd"] = sum(
        float(p.get("total_credit") or 0.0) for p in open_trades
    )
    portfolio["current_drawdown_pct"] = (
        max(0.0, (peak - current_equity) / peak * 100.0) if peak > 0 else 0.0
    )
    portfolio["kill_switch_active"] = bool(risk.get("kill_switch_active", False))
    out["portfolio"] = portfolio

    return out


def analyze_options_state(state: dict) -> OptionsAnalysisResult:
    """Analyze raw options state dict (legacy or live schema). Pure function."""
    state = _normalize_state(state)
    r = OptionsAnalysisResult()
    r.analysis_time = datetime.now(timezone.utc).isoformat()
    r.start_time = state.get("start_time")

    if r.start_time:
        try:
            start = datetime.fromisoformat(r.start_time)
            r.runtime_hours = (datetime.now(timezone.utc) - start).total_seconds() / 3600
        except (ValueError, TypeError):
            pass

    # ── Portfolio state ─────────────────────────────────────────────
    portfolio = state.get("portfolio", {})
    r.cumulative_pnl = portfolio.get("cumulative_pnl_usd", 0.0)
    r.premium_deployed = portfolio.get("total_premium_deployed_usd", 0.0)
    r.peak_equity = portfolio.get("peak_equity_usd", 0.0)
    r.drawdown_pct = portfolio.get("current_drawdown_pct", 0.0)
    r.kill_switch_active = portfolio.get("kill_switch_active", False)

    # ── Algo slots ──────────────────────────────────────────────────
    algos = state.get("algos", {})
    r.total_slots = len(algos)

    for key, slot in algos.items():
        strategy = slot.get("strategy", "")
        symbol = slot.get("symbol", "")
        sm = SlotMetrics(
            slot=key,
            strategy=strategy,
            symbol=symbol,
            trades=slot.get("total_trades", 0),
            wins=slot.get("winning_trades", 0),
            losses=slot.get("losing_trades", 0),
            realized_pnl=slot.get("total_pnl_usd", 0.0),
            consecutive_losses=slot.get("consecutive_losses", 0),
            circuit_breaker_active=slot.get("circuit_breaker_until") is not None,
        )
        if sm.trades > 0:
            sm.win_rate = (sm.wins / sm.trades) * 100
        r.slot_metrics[key] = sm

    # ── Open trades (unrealized P&L) ────────────────────────────────
    open_trades = state.get("open_trades", [])
    r.active_trades_count = len(open_trades)
    r.unrealized_pnl = sum(t.get("current_pnl_usd", 0.0) for t in open_trades)

    # ── Completed trades ────────────────────────────────────────────
    # Exclude simulator-only trades — yfinance ~15-min-delayed paper fills
    # produced inflated P&L numbers that misled the manager (e.g. a phantom
    # +$83 "best path" from 2026-03-20 SPY puts). The trade record itself
    # carries `[LIMITED-SIMULATOR]` in `data_quality_note` when this applies.
    raw_completed = state.get("completed_trades", [])
    completed = [
        t for t in raw_completed
        if "[LIMITED-SIMULATOR]" not in (t.get("data_quality_note") or "")
    ]
    r.total_trades = len(completed)

    wins = 0
    total_hold = 0.0
    total_fees = 0.0
    exit_reasons: Dict[str, int] = {}
    strat_data: Dict[str, StrategyMetrics] = {}
    sym_data: Dict[str, SymbolMetrics] = {}

    for t in completed:
        strategy = t.get("strategy", "unknown")
        symbol = t.get("symbol", "unknown")
        pnl = t.get("realized_pnl_usd", 0.0)
        entry_fee = t.get("entry_fee_usd", 0.0)
        exit_fee = t.get("exit_fee_usd", 0.0)
        fees = entry_fee + exit_fee
        hold = t.get("hold_days", 0.0)

        r.realized_pnl += pnl
        total_fees += fees
        total_hold += hold

        if pnl > 0:
            wins += 1

        # exit reason
        reason = t.get("exit_reason", "unknown") or "unknown"
        exit_reasons[reason] = exit_reasons.get(reason, 0) + 1

        # per-strategy
        if strategy not in strat_data:
            strat_data[strategy] = StrategyMetrics(strategy=strategy)
        sm = strat_data[strategy]
        sm.trades += 1
        sm.realized_pnl += pnl
        sm.fees += fees
        if pnl > 0:
            sm.wins += 1
        else:
            sm.losses += 1

        # per-symbol
        if symbol not in sym_data:
            sym_data[symbol] = SymbolMetrics(symbol=symbol)
        ym = sym_data[symbol]
        ym.trades += 1
        ym.realized_pnl += pnl
        ym.fees += fees
        if pnl > 0:
            ym.wins += 1
        else:
            ym.losses += 1

    r.fees_paid = total_fees
    r.exit_reason_breakdown = exit_reasons

    if r.total_trades > 0:
        r.win_rate = (wins / r.total_trades) * 100
        r.avg_hold_days = total_hold / r.total_trades

    # finalize per-strategy averages
    for sm in strat_data.values():
        if sm.trades > 0:
            sm.win_rate = (sm.wins / sm.trades) * 100
            sm.avg_hold_days = sum(
                t.get("hold_days", 0) for t in completed
                if t.get("strategy") == sm.strategy
            ) / sm.trades

    # finalize per-symbol averages
    for ym in sym_data.values():
        if ym.trades > 0:
            ym.win_rate = (ym.wins / ym.trades) * 100
            ym.avg_hold_days = sum(
                t.get("hold_days", 0) for t in completed
                if t.get("symbol") == ym.symbol
            ) / ym.trades

    # finalize per-slot hold times from completed trades
    for key, sm in r.slot_metrics.items():
        slot_trades = [
            t for t in completed
            if f"{t.get('strategy')}-{t.get('symbol')}" == key
        ]
        if slot_trades:
            sm.fees = sum(t.get("entry_fee_usd", 0) + t.get("exit_fee_usd", 0) for t in slot_trades)
            sm.avg_hold_days = sum(t.get("hold_days", 0) for t in slot_trades) / len(slot_trades)

    r.strategy_metrics = strat_data
    r.symbol_metrics = sym_data

    # ── Rankings (by strategy) ──────────────────────────────────────
    if strat_data:
        by_pnl = sorted(strat_data.values(), key=lambda s: s.realized_pnl, reverse=True)
        r.strongest_strategy = by_pnl[0].strategy
        r.weakest_strategy = by_pnl[-1].strategy

    # ── Confidence ──────────────────────────────────────────────────
    if r.total_trades >= 30:
        r.sample_confidence = "HIGH"
    elif r.total_trades >= 10:
        r.sample_confidence = "MEDIUM"
    else:
        r.sample_confidence = "LOW"

    # ── Recommendations ─────────────────────────────────────────────
    r.continue_recommendation = _continue_recommendation(r)
    r.narrow_recommendation = _narrow_recommendation(r, strat_data)

    return r


# ── Recommendation helpers ──────────────────────────────────────────


def _continue_recommendation(r: OptionsAnalysisResult) -> str:
    if r.kill_switch_active:
        return "PAUSED — kill switch active. Review drawdown before resuming."

    if r.total_trades < 5:
        return "INSUFFICIENT DATA — need at least 5 completed trades to evaluate. Continue running."

    if r.total_trades < 10:
        prefix = "EARLY SIGNAL"
    elif r.total_trades < 30:
        prefix = "TENTATIVE"
    else:
        prefix = "ASSESSED"

    if r.realized_pnl > 0 and r.win_rate >= 50:
        return f"{prefix} — positive P&L (${r.realized_pnl:.2f}) with {r.win_rate:.0f}% win rate. Worth continuing."
    if r.realized_pnl > 0 and r.win_rate < 50:
        return f"{prefix} — positive P&L (${r.realized_pnl:.2f}) but low win rate ({r.win_rate:.0f}%). Winners outsize losers. Monitor."
    if r.realized_pnl <= 0 and r.win_rate >= 50:
        return f"{prefix} — negative P&L (${r.realized_pnl:.2f}) despite {r.win_rate:.0f}% win rate. Losing trades too large. Review stops."
    return f"{prefix} — negative P&L (${r.realized_pnl:.2f}) with {r.win_rate:.0f}% win rate. Consider pausing if trend persists past 30 trades."


def _narrow_recommendation(r: OptionsAnalysisResult, strategies: Dict[str, StrategyMetrics]) -> str:
    if r.total_trades < 10:
        return "TOO EARLY — need at least 10 trades across strategies before narrowing."

    profitable = [s for s in strategies.values() if s.realized_pnl > 0 and s.trades >= 3]
    unprofitable = [s for s in strategies.values() if s.realized_pnl < 0 and s.trades >= 3]
    untested = [s for s in strategies.values() if s.trades < 3]

    parts = []
    if unprofitable:
        names = ", ".join(s.strategy for s in sorted(unprofitable, key=lambda x: x.realized_pnl))
        parts.append(f"Consider dropping: {names}")
    if profitable:
        names = ", ".join(s.strategy for s in sorted(profitable, key=lambda x: x.realized_pnl, reverse=True))
        parts.append(f"Keep: {names}")
    if untested:
        names = ", ".join(s.strategy for s in untested)
        parts.append(f"Need more data: {names}")

    if not parts:
        return "No clear narrowing signal yet."

    return " | ".join(parts)
