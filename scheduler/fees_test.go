package main

import (
	"math"
	"testing"
)

func TestCalculateFuturesFee(t *testing.T) {
	cases := []struct {
		contracts      int
		feePerContract float64
		want           float64
	}{
		{1, 1.50, 1.50},
		{2, 1.50, 3.00},
		{10, 0.50, 5.00},
		{0, 1.50, 0.00},
		{5, 0, 0.00},
	}
	for _, tc := range cases {
		got := CalculateFuturesFee(tc.contracts, tc.feePerContract)
		if math.Abs(got-tc.want) > 0.001 {
			t.Errorf("CalculateFuturesFee(%d, %.2f) = %.2f, want %.2f", tc.contracts, tc.feePerContract, got, tc.want)
		}
	}
}

func TestCalculatePlatformFuturesFee(t *testing.T) {
	// With FuturesConfig
	sc := StrategyConfig{
		FuturesConfig: &FuturesConfig{FeePerContract: 1.50},
	}
	got := CalculatePlatformFuturesFee(sc, 3)
	if math.Abs(got-4.50) > 0.001 {
		t.Errorf("expected 4.50, got %.2f", got)
	}

	// Without FuturesConfig
	sc2 := StrategyConfig{}
	got2 := CalculatePlatformFuturesFee(sc2, 3)
	if got2 != 0 {
		t.Errorf("expected 0 with no FuturesConfig, got %.2f", got2)
	}
}

// ── FillPrice tests ─────────────────────────────────────────────────────────

func TestFillPrice_AlwaysAdverse(t *testing.T) {
	cfg := SlippageConfig{BaseBps: 5.0, JitterBps: 0} // no jitter for deterministic test
	ctx := SlippageContext{Price: 10000.0}

	// Buys must fill ABOVE mid
	for i := 0; i < 100; i++ {
		fill := FillPrice("buy", ctx, cfg)
		if fill <= ctx.Price {
			t.Fatalf("buy fill %.6f <= mid %.2f on iteration %d", fill, ctx.Price, i)
		}
	}

	// Sells must fill BELOW mid
	for i := 0; i < 100; i++ {
		fill := FillPrice("sell", ctx, cfg)
		if fill >= ctx.Price {
			t.Fatalf("sell fill %.6f >= mid %.2f on iteration %d", fill, ctx.Price, i)
		}
	}
}

func TestFillPrice_LongShortSides(t *testing.T) {
	cfg := SlippageConfig{BaseBps: 5.0, JitterBps: 0}
	ctx := SlippageContext{Price: 10000.0}

	// "long" should behave like "buy"
	fill := FillPrice("long", ctx, cfg)
	if fill <= ctx.Price {
		t.Errorf("long fill %.6f should be above mid", fill)
	}

	// "short" should behave like "sell"
	fill = FillPrice("short", ctx, cfg)
	if fill >= ctx.Price {
		t.Errorf("short fill %.6f should be below mid", fill)
	}
}

func TestFillPrice_VolatilityIncreases(t *testing.T) {
	cfg := DefaultSlippageConfig()
	cfg.JitterBps = 0 // deterministic

	// Calm market: ATR/price = 0.3% (below 0.5% threshold → no vol penalty)
	ctxCalm := SlippageContext{Price: 10000.0, ATR: 30.0}
	fillCalm := FillPrice("buy", ctxCalm, cfg)

	// Volatile market: ATR/price = 1.5% (well above threshold)
	ctxVol := SlippageContext{Price: 10000.0, ATR: 150.0}
	fillVol := FillPrice("buy", ctxVol, cfg)

	// Volatile fill must be worse (higher for buy)
	if fillVol <= fillCalm {
		t.Errorf("volatile fill %.6f should be worse (higher) than calm fill %.6f", fillVol, fillCalm)
	}

	// Check magnitude: vol fill should be meaningfully worse
	slipCalm := (fillCalm - 10000.0) / 10000.0 * 10000 // bps
	slipVol := (fillVol - 10000.0) / 10000.0 * 10000    // bps
	if slipVol < slipCalm+5 {
		t.Errorf("vol slippage %.1f bps should be at least 5 bps more than calm %.1f bps", slipVol, slipCalm)
	}
}

func TestFillPrice_SizeScaling(t *testing.T) {
	cfg := DefaultSlippageConfig()
	cfg.JitterBps = 0
	cfg.SizeBpsPerSqrt = 2.0 // enable size scaling
	cfg.SizeRefUSD = 10000.0

	ctxSmall := SlippageContext{Price: 10000.0, OrderUSD: 100.0}
	ctxLarge := SlippageContext{Price: 10000.0, OrderUSD: 100000.0}

	fillSmall := FillPrice("buy", ctxSmall, cfg)
	fillLarge := FillPrice("buy", ctxLarge, cfg)

	if fillLarge <= fillSmall {
		t.Errorf("large order fill %.6f should be worse than small order fill %.6f", fillLarge, fillSmall)
	}
}

func TestFillPrice_ZeroPrice(t *testing.T) {
	cfg := DefaultSlippageConfig()
	ctx := SlippageContext{Price: 0}
	fill := FillPrice("buy", ctx, cfg)
	if fill != 0 {
		t.Errorf("expected 0 for zero price, got %.6f", fill)
	}
}

func TestFillPrice_BaseBps(t *testing.T) {
	cfg := SlippageConfig{BaseBps: 10.0, JitterBps: 0}
	ctx := SlippageContext{Price: 10000.0}

	fill := FillPrice("buy", ctx, cfg)
	expectedSlipBps := 10.0
	actualSlipBps := (fill - 10000.0) / 10000.0 * 10000
	if math.Abs(actualSlipBps-expectedSlipBps) > 0.01 {
		t.Errorf("expected %.1f bps slippage, got %.4f bps", expectedSlipBps, actualSlipBps)
	}
}

// ── GapFillPrice tests ──────────────────────────────────────────────────────

func TestGapFillPrice_LongStopGap(t *testing.T) {
	cfg := SlippageConfig{BaseBps: 3.0, JitterBps: 0}

	// Long position, stop at 9500, price gapped to 9400
	stopPrice := 9500.0
	gapPrice := 9400.0
	ctx := SlippageContext{Price: gapPrice}

	fill := GapFillPrice(stopPrice, gapPrice, "sell", ctx, cfg)

	// Fill should be at or below the gap price (worse for a sell)
	if fill > gapPrice {
		t.Errorf("gap fill %.2f should be <= gap price %.2f for long stop", fill, gapPrice)
	}
	// Fill should be worse than the stop level
	if fill > stopPrice {
		t.Errorf("gap fill %.2f should be <= stop level %.2f", fill, stopPrice)
	}
}

func TestGapFillPrice_ShortStopGap(t *testing.T) {
	cfg := SlippageConfig{BaseBps: 3.0, JitterBps: 0}

	// Short position, stop at 10500, price gapped to 10700
	stopPrice := 10500.0
	gapPrice := 10700.0
	ctx := SlippageContext{Price: gapPrice}

	fill := GapFillPrice(stopPrice, gapPrice, "buy", ctx, cfg)

	// Fill should be at or above the gap price (worse for a buy-to-cover)
	if fill < gapPrice {
		t.Errorf("gap fill %.2f should be >= gap price %.2f for short stop", fill, gapPrice)
	}
}

func TestGapFillPrice_NoGap(t *testing.T) {
	cfg := SlippageConfig{BaseBps: 3.0, JitterBps: 0}

	// Stop at 9500, price is 9500 exactly (no gap)
	stopPrice := 9500.0
	gapPrice := 9500.0
	ctx := SlippageContext{Price: gapPrice}

	fill := GapFillPrice(stopPrice, gapPrice, "sell", ctx, cfg)

	// Should just be stop level minus slippage
	if fill > stopPrice {
		t.Errorf("fill %.2f should be <= stop %.2f (adverse for sell)", fill, stopPrice)
	}
}

// ── extractATR tests ────────────────────────────────────────────────────────

func TestExtractATR(t *testing.T) {
	cases := []struct {
		name       string
		indicators map[string]interface{}
		want       float64
	}{
		{"nil map", nil, 0},
		{"empty map", map[string]interface{}{}, 0},
		{"atr present", map[string]interface{}{"atr": 150.5}, 150.5},
		{"ATR uppercase", map[string]interface{}{"ATR": 200.0}, 200.0},
		{"atr_14 present", map[string]interface{}{"atr_14": 100.0}, 100.0},
		{"atr zero", map[string]interface{}{"atr": 0.0}, 0},
		{"atr negative", map[string]interface{}{"atr": -5.0}, 0},
		{"atr int", map[string]interface{}{"atr": 42}, 42.0},
		{"no atr key", map[string]interface{}{"rsi": 55.0, "macd": 1.2}, 0},
	}
	for _, tc := range cases {
		got := extractATR(tc.indicators)
		if got != tc.want {
			t.Errorf("%s: extractATR() = %.2f, want %.2f", tc.name, got, tc.want)
		}
	}
}
