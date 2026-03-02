package main

import (
	"encoding/json"
	"strings"
	"testing"
)

func TestResolveChannel(t *testing.T) {
	channels := map[string]string{
		"spot":        "ch-spot",
		"hyperliquid": "ch-hl",
		"options":     "ch-opts",
	}

	// platform match takes priority
	if got := resolveChannel(channels, "hyperliquid", "perps"); got != "ch-hl" {
		t.Errorf("expected ch-hl, got %s", got)
	}
	// fall through to stratType
	if got := resolveChannel(channels, "binanceus", "spot"); got != "ch-spot" {
		t.Errorf("expected ch-spot, got %s", got)
	}
	// options type
	if got := resolveChannel(channels, "deribit", "options"); got != "ch-opts" {
		t.Errorf("expected ch-opts for deribit options, got %s", got)
	}
	// unknown → empty
	if got := resolveChannel(channels, "unknown", "unknown"); got != "" {
		t.Errorf("expected empty, got %s", got)
	}
}

func TestChannelKeyFromID(t *testing.T) {
	channels := map[string]string{
		"spot":        "111",
		"hyperliquid": "222",
	}
	if got := channelKeyFromID(channels, "111"); got != "spot" {
		t.Errorf("expected spot, got %s", got)
	}
	if got := channelKeyFromID(channels, "222"); got != "hyperliquid" {
		t.Errorf("expected hyperliquid, got %s", got)
	}
	// unknown channel ID falls back to itself
	if got := channelKeyFromID(channels, "999"); got != "999" {
		t.Errorf("expected 999, got %s", got)
	}
}

func TestIsOptionsType(t *testing.T) {
	spot := []StrategyConfig{{Type: "spot"}, {Type: "perps"}}
	opts := []StrategyConfig{{Type: "spot"}, {Type: "options"}}
	if isOptionsType(spot) {
		t.Error("expected false for spot/perps only")
	}
	if !isOptionsType(opts) {
		t.Error("expected true when options present")
	}
}

func TestExtractAsset(t *testing.T) {
	cases := []struct {
		sc   StrategyConfig
		want string
	}{
		// spot: Args[1] is "BTC/USDT" → strip suffix → "BTC"
		{StrategyConfig{Type: "spot", Args: []string{"sma_crossover", "BTC/USDT"}}, "BTC"},
		// options: Args[1] is the underlying symbol
		{StrategyConfig{Type: "options", Args: []string{"wheel", "ETH", "--platform=deribit"}}, "ETH"},
		// perps: Args[1] is coin name
		{StrategyConfig{Type: "perps", Args: []string{"momentum", "SOL", "1h"}}, "SOL"},
		// perps BNB
		{StrategyConfig{Type: "perps", Args: []string{"rsi", "BNB", "1h"}}, "BNB"},
		// empty args → ""
		{StrategyConfig{Type: "spot", Args: []string{}}, ""},
		// only one arg → ""
		{StrategyConfig{Type: "perps", Args: []string{"strategy"}}, ""},
	}
	for _, c := range cases {
		got := extractAsset(c.sc)
		if got != c.want {
			t.Errorf("extractAsset(%v) = %q, want %q", c.sc.Args, got, c.want)
		}
	}
}

func TestGroupByAsset(t *testing.T) {
	strats := []StrategyConfig{
		{ID: "hl-rsi-eth", Type: "perps", Args: []string{"rsi", "ETH", "1h"}},
		{ID: "hl-mom-btc", Type: "perps", Args: []string{"momentum", "BTC", "1h"}},
		{ID: "hl-ema-sol", Type: "perps", Args: []string{"ema", "SOL", "1h"}},
		{ID: "hl-rsi-bnb", Type: "perps", Args: []string{"rsi", "BNB", "1h"}},
		{ID: "hl-sma-btc", Type: "perps", Args: []string{"sma", "BTC", "1h"}},
	}
	groups, keys := groupByAsset(strats)

	// 4 distinct assets
	if len(keys) != 4 {
		t.Fatalf("expected 4 asset keys, got %d: %v", len(keys), keys)
	}
	// BTC first, ETH second, SOL third, BNB fourth
	if keys[0] != "BTC" || keys[1] != "ETH" || keys[2] != "SOL" || keys[3] != "BNB" {
		t.Errorf("unexpected key order: %v", keys)
	}
	// BTC group has 2 strategies
	if len(groups["BTC"]) != 2 {
		t.Errorf("expected 2 BTC strategies, got %d", len(groups["BTC"]))
	}

	// Single asset case
	single := []StrategyConfig{
		{ID: "hl-rsi-eth", Type: "perps", Args: []string{"rsi", "ETH", "1h"}},
	}
	_, keys2 := groupByAsset(single)
	if len(keys2) != 1 || keys2[0] != "ETH" {
		t.Errorf("single asset: expected [ETH], got %v", keys2)
	}
}

func TestFormatCategorySummary_WithAsset(t *testing.T) {
	strats := []StrategyConfig{
		{ID: "hl-rsi-btc", Type: "perps", Args: []string{"rsi", "BTC", "1h"}, Capital: 1000},
	}
	state := &AppState{
		Strategies: map[string]*StrategyState{
			"hl-rsi-btc": {Cash: 1000},
		},
	}
	prices := map[string]float64{"BTC/USDT": 50000, "ETH/USDT": 3000}

	// With asset — title should contain " — BTC" and only BTC price shown
	msg := FormatCategorySummary(1, 0, 1, 0, 1000, prices, nil, strats, state, "hyperliquid", "BTC")
	if !strings.Contains(msg, "— BTC") {
		t.Errorf("expected '— BTC' in title, got:\n%s", msg)
	}
	if strings.Contains(msg, "ETH") {
		t.Errorf("ETH price should be filtered out for asset=BTC, got:\n%s", msg)
	}

	// Without asset — no suffix in title
	msg2 := FormatCategorySummary(1, 0, 1, 0, 1000, prices, nil, strats, state, "hyperliquid", "")
	if strings.Contains(msg2, "— ") {
		t.Errorf("expected no asset suffix when asset='', got:\n%s", msg2)
	}
}

func TestDiscordChannels_BackwardsCompatJSON(t *testing.T) {
	// Old config format {"spot":"x","options":"y"} should still parse into map[string]string.
	raw := `{"enabled":true,"token":"","channels":{"spot":"ch1","options":"ch2"}}`
	var dc DiscordConfig
	if err := json.Unmarshal([]byte(raw), &dc); err != nil {
		t.Fatalf("unmarshal failed: %v", err)
	}
	if dc.Channels["spot"] != "ch1" {
		t.Errorf("expected ch1, got %s", dc.Channels["spot"])
	}
	if dc.Channels["options"] != "ch2" {
		t.Errorf("expected ch2, got %s", dc.Channels["options"])
	}
	// New key works too
	raw2 := `{"enabled":true,"token":"","channels":{"spot":"ch1","options":"ch2","hyperliquid":"ch3"}}`
	var dc2 DiscordConfig
	if err := json.Unmarshal([]byte(raw2), &dc2); err != nil {
		t.Fatalf("unmarshal failed: %v", err)
	}
	if dc2.Channels["hyperliquid"] != "ch3" {
		t.Errorf("expected ch3, got %s", dc2.Channels["hyperliquid"])
	}
}
