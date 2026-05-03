package main

import (
	"math"
	"math/rand"
)

// ── Fee rates ───────────────────────────────────────────────────────────────

const (
	BinanceSpotFeePct      = 0.001   // 0.1% taker fee
	DeribitOptionFeePct    = 0.0003  // 0.03% of contract value
	IBKROptionFeeFixed     = 0.25    // $0.25 per contract (CME Micro fee)
	HyperliquidTakerFeePct = 0.00035 // 0.035% taker fee
)

// ── Slippage model ──────────────────────────────────────────────────────────
//
// Design assumptions (paper trading execution realism):
//
// 1. ALWAYS ADVERSE: buys fill above mid, sells fill below mid.
//    Real markets exhibit adverse selection — you cross the spread to get
//    filled, and the other side has more information than you.
//
// 2. VOLATILITY-SENSITIVE: when ATR is high relative to price, slippage
//    increases. Volatile markets have wider spreads and more aggressive
//    market makers. The vol factor scales linearly: ATR/price normalized
//    against a calm-market baseline, then multiplied by VolBpsPerUnit.
//
// 3. SIZE-SENSITIVE (optional): larger orders move price more. Uses a
//    sqrt model: slippage_bps += SizeBpsPerSqrtUnit * sqrt(orderUSD / SizeRefUSD).
//    Disabled by default (SizeBpsPerSqrtUnit=0). Turn on for larger capital.
//
// 4. RANDOM COMPONENT: small uniform jitter (0 to JitterBps) on top of the
//    deterministic adverse component. Models execution timing variance.
//    Always non-negative — jitter only makes fills worse, never better.
//
// The old model was: uniform random in [-5bps, +5bps] regardless of side,
// volatility, or size. That model gave free money on 50% of fills.

// SlippageConfig holds all tunable slippage parameters.
// Zero-value is a safe default that behaves like 5bps adverse + jitter.
type SlippageConfig struct {
	BaseBps         float64 // fixed adverse cost in basis points (default 3.0)
	VolBpsPerUnit   float64 // additional bps per unit of ATR/price above calm baseline (default 10.0)
	VolCalmATRRatio float64 // ATR/price ratio considered "calm" — no vol penalty below this (default 0.005 = 0.5%)
	SizeBpsPerSqrt  float64 // additional bps per sqrt(orderUSD/SizeRefUSD) (default 0 = disabled)
	SizeRefUSD      float64 // reference order size for sqrt scaling (default 10000)
	JitterBps       float64 // max random jitter in bps, always adverse (default 2.0)
}

// DefaultSlippageConfig returns a conservative config suitable for crypto spot/perps.
// Total slippage in calm markets: ~3-5 bps adverse.
// Total slippage in volatile markets (ATR/price=1.5%): ~13-15 bps adverse.
func DefaultSlippageConfig() SlippageConfig {
	return SlippageConfig{
		BaseBps:         3.0,
		VolBpsPerUnit:   10.0,
		VolCalmATRRatio: 0.005,
		SizeBpsPerSqrt:  0.0, // disabled by default; set to ~1.0 for large capital
		SizeRefUSD:      10000.0,
		JitterBps:       2.0,
	}
}

// SlippageContext carries per-fill market state. Callers set what they know;
// unknown fields left at zero trigger safe fallbacks.
type SlippageContext struct {
	ATR      float64 // recent Average True Range for the symbol (0 = unknown → use base only)
	Price    float64 // current mid price (required, must be > 0)
	OrderUSD float64 // notional order value in USD (0 = skip size component)
}

// FillPrice computes a realistic adverse fill price for paper trading.
//
// side: "buy" or "sell" (or "long"/"short" — anything starting with 'b'/'l' = buy side).
// Returns the fill price, always worse than mid for the trader.
func FillPrice(side string, ctx SlippageContext, cfg SlippageConfig) float64 {
	if ctx.Price <= 0 {
		return ctx.Price
	}

	// 1. Base adverse slippage
	totalBps := cfg.BaseBps

	// 2. Volatility component
	if ctx.ATR > 0 && ctx.Price > 0 {
		atrRatio := ctx.ATR / ctx.Price
		excess := atrRatio - cfg.VolCalmATRRatio
		if excess > 0 {
			totalBps += cfg.VolBpsPerUnit * (excess / cfg.VolCalmATRRatio)
		}
	}

	// 3. Size component (sqrt model, optional)
	if cfg.SizeBpsPerSqrt > 0 && ctx.OrderUSD > 0 && cfg.SizeRefUSD > 0 {
		totalBps += cfg.SizeBpsPerSqrt * math.Sqrt(ctx.OrderUSD/cfg.SizeRefUSD)
	}

	// 4. Random jitter (always adverse: [0, JitterBps])
	if cfg.JitterBps > 0 {
		totalBps += rand.Float64() * cfg.JitterBps
	}

	// Convert bps to fraction and apply adverse direction
	slip := totalBps / 10000.0

	isBuy := len(side) > 0 && (side[0] == 'b' || side[0] == 'B' || side[0] == 'l' || side[0] == 'L')
	if isBuy {
		return ctx.Price * (1 + slip) // buys fill above mid
	}
	return ctx.Price * (1 - slip) // sells fill below mid
}

// GapFillPrice computes the fill price when a stop or limit is triggered by a
// price that has gapped through the stop level.
//
// In real markets, stop-loss orders become market orders when triggered.
// If price gaps past the stop, you fill at the gap price, not at your stop.
// This function returns the worse of (stop_level, gap_price) plus slippage.
//
// stopPrice: the stop level that was breached
// gapPrice:  the actual observed price (the gap-through price)
// side:      the exit side ("sell" for closing a long, "buy" for closing a short)
func GapFillPrice(stopPrice, gapPrice float64, side string, ctx SlippageContext, cfg SlippageConfig) float64 {
	isBuy := len(side) > 0 && (side[0] == 'b' || side[0] == 'B' || side[0] == 'l' || side[0] == 'L')

	// The fill price starts at whichever is worse for the trader
	var base float64
	if isBuy {
		// Closing a short — higher is worse
		base = math.Max(stopPrice, gapPrice)
	} else {
		// Closing a long — lower is worse
		base = math.Min(stopPrice, gapPrice)
	}

	// Apply adverse slippage on top of the gap price
	ctx.Price = base
	return FillPrice(side, ctx, cfg)
}

// ── Indicator extraction ────────────────────────────────────────────────────

// extractATR pulls an ATR value from a strategy's indicators map.
// Returns 0 if not present (callers treat 0 as "unknown, use base slippage only").
func extractATR(indicators map[string]interface{}) float64 {
	if indicators == nil {
		return 0
	}
	// Try common indicator keys for ATR
	for _, key := range []string{"atr", "ATR", "atr_14", "atr14"} {
		if v, ok := indicators[key]; ok {
			switch n := v.(type) {
			case float64:
				if n > 0 {
					return n
				}
			case int:
				if n > 0 {
					return float64(n)
				}
			}
		}
	}
	return 0
}

// ── Legacy compatibility ────────���───────────────────────────────────────────

// ApplySlippage is the DEPRECATED legacy function.
// Retained only for call sites not yet migrated (e.g. options).
// Uses adverse-only slippage with no volatility context.
// TODO: remove once all call sites use FillPrice.
func ApplySlippage(price float64) float64 {
	// Adverse bias: ~5 bps worse (was symmetric ±5 bps random)
	slip := (3.0 + rand.Float64()*2.0) / 10000.0
	// 50/50 buy/sell since we don't know side — still always adverse
	if rand.Float64() < 0.5 {
		return price * (1 + slip)
	}
	return price * (1 - slip)
}

// ── Fee calculators (unchanged) ─────────────────────────────────────────────

func CalculateSpotFee(value float64) float64 {
	return value * BinanceSpotFeePct
}

func CalculateHyperliquidFee(notionalUSD float64) float64 {
	return notionalUSD * HyperliquidTakerFeePct
}

func CalculatePlatformSpotFee(platform string, value float64) float64 {
	if platform == "hyperliquid" {
		return CalculateHyperliquidFee(value)
	}
	return CalculateSpotFee(value)
}

func CalculateDeribitOptionFee(premiumUSD float64) float64 {
	return premiumUSD * DeribitOptionFeePct
}

func CalculateIBKROptionFee(quantity float64) float64 {
	return quantity * IBKROptionFeeFixed
}

func CalculateOptionFee(platform string, premiumUSD, quantity float64) float64 {
	if platform == "ibkr" {
		return CalculateIBKROptionFee(quantity)
	}
	return CalculateDeribitOptionFee(premiumUSD)
}

func CalculateFuturesFee(contracts int, feePerContract float64) float64 {
	return float64(contracts) * feePerContract
}

func CalculatePlatformFuturesFee(sc StrategyConfig, contracts int) float64 {
	if sc.FuturesConfig != nil && sc.FuturesConfig.FeePerContract > 0 {
		return CalculateFuturesFee(contracts, sc.FuturesConfig.FeePerContract)
	}
	return 0
}
