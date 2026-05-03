"""
research/lint.py — Strategy sanity-check / lint pass.

Runs each candidate through a series of structural checks on synthetic data
before allowing it into the paper-trading research lane.

Checks:
  1. Registration    — strategy name exists in STRATEGY_REGISTRY
  2. Runs without error — apply_strategy() completes on 200 synthetic bars
  3. Signal column   — result has a 'signal' column
  4. Non-trivial     — at least 1 non-zero signal across 200 bars
  5. Not over-firing — signals on < 60% of bars (avoids always-in strategies)
  6. Directional     — produces BOTH buy and sell signals (not one-sided only)
  7. Look-ahead      — shifting by 1 from the closing bar must not increase
                       signal count (basic sanity, not a rigorous proof)
  8. Phase detection — for strategies whose hypothesis mentions 'two-phase',
                       'retest', or 'breakout': verify signal cannot fire on
                       the same bar as a newly crossed level.

Each check returns PASS, WARN, or FAIL with a short reason.
Overall verdict: PASS (all PASS/WARN), CONDITIONAL (any WARN), FAIL (any FAIL).
"""

from __future__ import annotations
import sys
import os
import inspect
import numpy as np
import pandas as pd
from dataclasses import dataclass, field
from typing import List, Tuple

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'shared_strategies', 'spot'))


@dataclass
class CheckResult:
    name: str
    status: str   # PASS | WARN | FAIL
    reason: str


@dataclass
class LintResult:
    strategy: str
    checks: List[CheckResult] = field(default_factory=list)
    verdict: str = "PASS"   # PASS | CONDITIONAL | FAIL
    summary: str = ""


def _make_synthetic_df(n: int = 200, seed: int = 42) -> pd.DataFrame:
    """200-bar synthetic OHLCV with mild trend and volatility."""
    rng = np.random.default_rng(seed)
    returns = rng.normal(0, 0.005, n)
    close = 100.0 * np.cumprod(1 + returns)
    noise = rng.uniform(0.001, 0.008, n)
    high = close * (1 + noise)
    low  = close * (1 - noise)
    open_ = close * (1 + rng.normal(0, 0.003, n))
    volume = rng.uniform(500, 5000, n)
    return pd.DataFrame({
        "open": open_, "high": high, "low": low,
        "close": close, "volume": volume,
    })


def _make_trending_df(n: int = 200, seed: int = 7) -> pd.DataFrame:
    """Trending synthetic data to stress-test range-breakout strategies."""
    rng = np.random.default_rng(seed)
    returns = rng.normal(0.003, 0.005, n)   # upward drift
    close = 100.0 * np.cumprod(1 + returns)
    noise = rng.uniform(0.001, 0.006, n)
    high = close * (1 + noise)
    low  = close * (1 - noise)
    open_ = close * (1 + rng.normal(0, 0.002, n))
    volume = rng.uniform(500, 5000, n)
    return pd.DataFrame({
        "open": open_, "high": high, "low": low,
        "close": close, "volume": volume,
    })


def _run_safe(strategy_name: str, df: pd.DataFrame):
    """Apply strategy, return (result_df, error_str)."""
    try:
        from strategies import apply_strategy
        result = apply_strategy(strategy_name, df)
        return result, None
    except Exception as e:
        return None, str(e)


def lint_strategy(strategy_name: str, hypothesis: str = "") -> LintResult:
    result = LintResult(strategy=strategy_name)
    checks = result.checks

    # ── Check 1: registration ───────────────────────────────────────────────
    try:
        from strategies import STRATEGY_REGISTRY
        if strategy_name not in STRATEGY_REGISTRY:
            checks.append(CheckResult("registration", "FAIL",
                f"'{strategy_name}' not found in STRATEGY_REGISTRY"))
            result.verdict = "FAIL"
            result.summary = "Strategy not registered — cannot proceed"
            return result
        checks.append(CheckResult("registration", "PASS",
            f"Found in registry: {STRATEGY_REGISTRY[strategy_name]['description'][:60]}"))
    except Exception as e:
        checks.append(CheckResult("registration", "FAIL", str(e)))
        result.verdict = "FAIL"
        return result

    # ── Check 2: runs without error ─────────────────────────────────────────
    df_flat    = _make_synthetic_df()
    df_trend   = _make_trending_df()
    res_flat,  err_flat   = _run_safe(strategy_name, df_flat)
    res_trend, err_trend  = _run_safe(strategy_name, df_trend)

    if err_flat or err_trend:
        err = err_flat or err_trend
        checks.append(CheckResult("executes", "FAIL", f"Exception: {err[:120]}"))
        result.verdict = "FAIL"
        result.summary = "Strategy crashes on synthetic data"
        return result
    checks.append(CheckResult("executes", "PASS", "No exception on flat or trending data"))

    # ── Check 3: signal column present ──────────────────────────────────────
    if "signal" not in res_flat.columns:
        checks.append(CheckResult("signal_column", "FAIL", "No 'signal' column in output"))
        result.verdict = "FAIL"
        return result
    checks.append(CheckResult("signal_column", "PASS", "Output contains 'signal' column"))

    # ── Check 4: non-trivial (fires at least once) ───────────────────────────
    sig_flat  = res_flat["signal"].values
    sig_trend = res_trend["signal"].values
    nz_flat  = int(np.count_nonzero(sig_flat))
    nz_trend = int(np.count_nonzero(sig_trend))
    total_nz = nz_flat + nz_trend

    if total_nz == 0:
        checks.append(CheckResult("non_trivial", "FAIL",
            "Zero signals on both flat and trending data (200 bars each) — strategy never fires"))
        result.verdict = "FAIL"
        result.summary = "Strategy produces no signals — likely over-constrained or broken logic"
        return result
    if total_nz < 2:
        checks.append(CheckResult("non_trivial", "WARN",
            f"Only {total_nz} signal(s) across 400 bars — very sparse, may not accumulate trades"))
        _set_min(result, "CONDITIONAL")
    else:
        checks.append(CheckResult("non_trivial", "PASS",
            f"{nz_flat} signals on flat data, {nz_trend} on trending data"))

    # ── Check 5: not over-firing (< 60% of bars) ────────────────────────────
    n = len(sig_flat)
    fire_pct_flat  = nz_flat  / n * 100
    fire_pct_trend = nz_trend / n * 100
    if fire_pct_flat > 60 or fire_pct_trend > 60:
        checks.append(CheckResult("fire_rate", "FAIL",
            f"Over-fires: {fire_pct_flat:.0f}% flat / {fire_pct_trend:.0f}% trending — always-in strategy"))
        result.verdict = "FAIL"
        return result
    elif fire_pct_flat > 30 or fire_pct_trend > 30:
        checks.append(CheckResult("fire_rate", "WARN",
            f"High fire rate: {fire_pct_flat:.0f}% flat / {fire_pct_trend:.0f}% trending — may whipsaw"))
        _set_min(result, "CONDITIONAL")
    else:
        checks.append(CheckResult("fire_rate", "PASS",
            f"{fire_pct_flat:.1f}% flat / {fire_pct_trend:.1f}% trending"))

    # ── Check 6: directional (both buy and sell signals) ────────────────────
    has_buy  = (np.any(sig_flat > 0)  or np.any(sig_trend > 0))
    has_sell = (np.any(sig_flat < 0)  or np.any(sig_trend < 0))
    if not has_buy:
        checks.append(CheckResult("directional", "WARN",
            "No buy signals seen — strategy may be short-only or unresponsive to upside"))
        _set_min(result, "CONDITIONAL")
    elif not has_sell:
        checks.append(CheckResult("directional", "WARN",
            "No sell signals seen — strategy may be long-only or unresponsive to downside"))
        _set_min(result, "CONDITIONAL")
    else:
        checks.append(CheckResult("directional", "PASS", "Produces both buy and sell signals"))

    # ── Check 7: basic look-ahead proxy ─────────────────────────────────────
    # Shift close by 1 (simulate using future data) — signal count should not
    # increase significantly. A big jump suggests the signals are easier to
    # generate with future prices, which is a look-ahead red flag.
    df_shifted = df_flat.copy()
    df_shifted["close"] = df_shifted["close"].shift(1).ffill()
    res_shifted, err_shifted = _run_safe(strategy_name, df_shifted)
    if err_shifted is None and "signal" in res_shifted.columns:
        nz_shifted = int(np.count_nonzero(res_shifted["signal"].values))
        ratio = nz_shifted / max(nz_flat, 1)
        if ratio > 2.5:
            checks.append(CheckResult("look_ahead_proxy", "WARN",
                f"Signals jump {nz_flat}→{nz_shifted} when close is shifted forward — "
                "possible look-ahead or window alignment issue"))
            _set_min(result, "CONDITIONAL")
        else:
            checks.append(CheckResult("look_ahead_proxy", "PASS",
                f"Signal count stable when close shifted ({nz_flat} vs {nz_shifted})"))
    else:
        checks.append(CheckResult("look_ahead_proxy", "WARN",
            "Could not run shifted variant — skipping check"))
        _set_min(result, "CONDITIONAL")

    # ── Check 8: two-phase verification (for breakout/retest strategies) ────
    hyp_lower = hypothesis.lower()
    # Only check phase separation for strategies that explicitly claim two-phase
    # or retest behaviour. Plain breakout strategies (ORB, Donchian) signal
    # ON the breakout bar by design — do not penalize them for that.
    is_multiphase = any(kw in hyp_lower for kw in
                        ("two-phase", "retest", "break-and-retest"))
    if is_multiphase:
        # Build a synthetic dataset where a clean breakout fires on bar 20,
        # and check that no signal fires on exactly that bar.
        df_phase = _make_synthetic_df(200, seed=99)
        df_phase = df_phase.copy()
        # Force a breakout on bar 20 by pumping close well above prior high
        df_phase.loc[20, "close"] = df_phase["close"].iloc[:20].max() * 1.10
        df_phase.loc[20, "high"]  = df_phase.loc[20, "close"] * 1.005
        res_phase, err_phase = _run_safe(strategy_name, df_phase)
        if err_phase is None and "signal" in res_phase.columns:
            sig_on_breakout_bar = int(res_phase["signal"].iloc[20])
            if sig_on_breakout_bar != 0:
                checks.append(CheckResult("two_phase", "FAIL",
                    f"Signal fires ON the breakout bar itself (bar 20 signal={sig_on_breakout_bar}) — "
                    "phases are collapsed, not separated"))
                result.verdict = "FAIL"
                result.summary = "Two-phase logic is collapsed — breakout and entry fire on same bar"
            else:
                checks.append(CheckResult("two_phase", "PASS",
                    "Signal does not fire on breakout bar — phases are correctly separated"))
        else:
            checks.append(CheckResult("two_phase", "WARN",
                "Phase-separation check skipped (error running variant)"))
            _set_min(result, "CONDITIONAL")

    # ── Final verdict ────────────────────────────────────────────────────────
    if result.verdict == "PASS":
        result.summary = "All checks passed — acceptable for research lane"
    elif result.verdict == "CONDITIONAL":
        warns = [c for c in checks if c.status == "WARN"]
        result.summary = f"{len(warns)} warning(s): " + "; ".join(c.name for c in warns)

    return result


def _set_min(result: LintResult, level: str):
    order = {"PASS": 0, "CONDITIONAL": 1, "FAIL": 2}
    if order.get(level, 0) > order.get(result.verdict, 0):
        result.verdict = level


def lint_all(candidates=None) -> List[LintResult]:
    if candidates is None:
        from research.registry import get_enabled
        candidates = get_enabled()
    results = []
    for c in candidates:
        lr = lint_strategy(c.name, c.hypothesis)
        results.append(lr)
    return results


def print_lint_results(results: List[LintResult]):
    WIDTH = 72
    print("=" * WIDTH)
    print("STRATEGY LINT REPORT")
    print("=" * WIDTH)
    for lr in results:
        icon = {"PASS": "✓", "CONDITIONAL": "~", "FAIL": "✗"}.get(lr.verdict, "?")
        print(f"\n[{icon}] {lr.strategy.upper()}  —  {lr.verdict}")
        print(f"    {lr.summary}")
        for c in lr.checks:
            sym = {"PASS": " ", "WARN": "!", "FAIL": "✗"}.get(c.status, "?")
            print(f"    [{sym}] {c.name:<22} {c.reason}")
    print("\n" + "=" * WIDTH)
    verdicts = [lr.verdict for lr in results]
    print(f"Summary: {verdicts.count('PASS')} PASS  "
          f"{verdicts.count('CONDITIONAL')} CONDITIONAL  "
          f"{verdicts.count('FAIL')} FAIL")
    print("=" * WIDTH)
