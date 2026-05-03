package main

// Regime-based strategy gating.
//
// Blocks signals that are mismatched to current market conditions.
// If regime is missing or "unknown", all signals are allowed (backward compatible).

// Strategy classification — matches signal_trading/config.py.
var trendStrategies = map[string]bool{
	"sma_crossover":  true,
	"ema_crossover":  true,
	"momentum":       true,
	"triple_ema":     true,
	"macd":           true,
	"volume_weighted": true,
	"rsi_macd_combo": true,
}

var rangeStrategies = map[string]bool{
	"rsi":             true,
	"bollinger_bands": true,
	"mean_reversion":  true,
}

// CheckRegimeGate decides whether a signal should be executed given the
// current market regime. Returns (allowed, reason).
//
// Rules:
//   - "" or "unknown"       → allow (no data to filter on)
//   - "volatile"            → block all
//   - "trend_up/trend_down" → allow trend strategies, block range strategies
//   - "range"               → allow range strategies, block trend strategies
//   - Unclassified strategies are always allowed regardless of regime
func CheckRegimeGate(strategy, regime string) (bool, string) {
	if regime == "" || regime == "unknown" {
		return true, ""
	}

	if regime == "volatile" {
		return false, "volatile regime — blocking all new entries"
	}

	isTrend := trendStrategies[strategy]
	isRange := rangeStrategies[strategy]

	// Unclassified strategies (pairs_spread, orb, etc.) pass through
	if !isTrend && !isRange {
		return true, ""
	}

	switch regime {
	case "trend_up", "trend_down":
		if isRange {
			return false, strategy + " is a range strategy — blocked in " + regime + " regime"
		}
	case "range":
		if isTrend {
			return false, strategy + " is a trend strategy — blocked in range regime"
		}
	}

	return true, ""
}
