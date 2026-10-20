"""Engine fix tests (bug-hunt round 2 + signal audit 2026-10-21) — no network.

Covers: MTF set 1h/30m/15m/5m with trend-based legs (hard gate on 1h,
weighted score that rejects in chop), the RANGING regime brake, BTC 1h
context in btc_align, the discriminative quality scale,
market.get_klines drops the still-forming last candle so analysis
never repaints, and market.equity_usdt returns margin balance (not
available balance) for the daily loss cap.
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import signal_engine
from signal_engine import btc_align, evaluate_symbol, mtf_check, quality_score


def _trend_candles(n=120, bull=True):
    out = []
    p = 100.0
    for i in range(n):
        p *= 1.002 if bull else 0.998
        o = p / 1.001
        out.append({"open": o, "high": p * 1.001, "low": o * 0.999,
                    "close": p, "volume": 1000 + i})
    return out


def _flat_candles(n=120, price=100.0):
    return [{"open": price, "high": price, "low": price,
             "close": price, "volume": 1000} for _ in range(n)]


class _MTFMarket:
    """Per-interval trend control: 'bull' | 'bear' | 'flat' (NEUTRAL)."""

    def __init__(self, trends=None):
        self.trends = trends or {}

    def get_klines(self, symbol, interval, limit=None):
        t = self.trends.get(interval, "bull")
        if t == "flat":
            return _flat_candles()
        return _trend_candles(bull=(t == "bull"))


def _mtf(trends=None):
    return mtf_check(_MTFMarket(trends), "TESTUSDT", "BULLISH",
                     {"signal": "BULLISH WATCH"})


def test_mtf_trend_legs_reject_in_chop():
    """Audit regression 2026-10-21: with voting legs the MTF gate
    rejected 0 of ~160 live evaluations. Trend legs: 1h trend aligned
    (0.40) + 15m signal leg (0.20), but 30m and 5m trends NEUTRAL
    (flat) -> score 0.60 < 0.70 -> must FAIL."""
    ok, note, score = _mtf({"30m": "flat", "5m": "flat"})
    assert ok is False, note
    assert "MTF score" in note and abs(score - 0.60) < 1e-9


def test_mtf_trend_legs_pass_when_all_aligned():
    ok, note, score = _mtf({})
    assert ok is True, note
    assert abs(score - 1.0) < 1e-9


def test_mtf_trend_legs_partial_support():
    # 30m trend aligned, 5m flat: 0.40+0.25+0.20 = 0.85 -> pass
    ok, note, score = _mtf({"5m": "flat"})
    assert ok is True and abs(score - 0.85) < 1e-9, note
    # 30m flat, 5m trend aligned: 0.40+0.20+0.15 = 0.75 -> pass
    ok, note, score = _mtf({"30m": "flat"})
    assert ok is True and abs(score - 0.75) < 1e-9, note


def test_mtf_hard_gate_rejects_when_1h_opposes():
    # 1h trend BEARISH against a BULLISH signal: hard gate fails even
    # though 30m/15m/5m are all aligned.
    ok, note, score = _mtf({"1h": "bear"})
    assert ok is False, note
    assert "hard-gate" in note


def test_quality_score_discriminates():
    """Audit regression: the old scale put strength 5 at base 90 so
    MIN_QUALITY=62 never bit. The rebuilt scale must spread: a perfect
    setup scores high, a weak one lands BELOW the quality gate."""
    strong = {"strength": 5, "regime": "TRENDING BULLISH",
              "volume_ratio": 1.4, "efficiency_ratio": 0.4,
              "plus_di": 30.0, "minus_di": 10.0}
    assert quality_score(strong, "BULLISH", 1.0) >= 90
    weak = {"strength": 3, "regime": "MIXED", "volume_ratio": 1.0,
            "efficiency_ratio": 0.1, "plus_di": 10.0, "minus_di": 30.0}
    assert quality_score(weak, "BULLISH", 0.75) < 62
    # regime swing (+8 trending vs -12 ranging = 20) measured at
    # strength 4 so neither side hits the 98 clamp
    s4 = dict(strong, strength=4)
    ranging4 = dict(s4, regime="RANGING")
    assert quality_score(s4, "BULLISH", 1.0) - \
        quality_score(ranging4, "BULLISH", 1.0) >= 20


class _EvalMarket:
    def get_24h_quote_volume(self, symbol):
        return 50_000_000.0

    def get_klines(self, symbol, interval, limit=None):
        return _trend_candles(bull=True)

    def get_ticker_price(self, symbol):
        return 100.0


def _prepared_a15(regime):
    plan = {"entry": 100.0, "entry_low": 99.9, "entry_high": 100.1,
            "stop_loss": 98.0, "tp1": 103.0, "tp2": 104.5, "tp3": 106.0,
            "rr1": 1.5, "risk": 2.0, "atr": 1.0}
    return {"signal": "BULLISH WATCH", "strength": 4, "regime": regime,
            "trade_plan": plan, "rsi": 55.0, "adx": 25.0,
            "volume_ratio": 1.2, "efficiency_ratio": 0.3,
            "plus_di": 25.0, "minus_di": 15.0, "price": 100.0,
            "ema21": 99.0, "atr": 1.0, "reasons": ["test"],
            "doji_ratio": 0.1, "direction_flips": 0}


def test_regime_brake_blocks_ranging():
    orig = signal_engine.analyze_candles
    signal_engine.analyze_candles = (
        lambda sym, iv, candles: _prepared_a15("RANGING"))
    try:
        sig, reason = evaluate_symbol("TESTUSDT", _EvalMarket())
    finally:
        signal_engine.analyze_candles = orig
    assert sig is None and "regime" in reason, reason


def test_full_path_passes_when_trending():
    orig = signal_engine.analyze_candles
    signal_engine.analyze_candles = (
        lambda sym, iv, candles: _prepared_a15("TRENDING BULLISH"))
    try:
        sig, note = evaluate_symbol("TESTUSDT", _EvalMarket())
    finally:
        signal_engine.analyze_candles = orig
    assert sig is not None, note
    assert sig["quality"] >= 62


class _BTCMarket:
    def __init__(self, h1_bull):
        self.h1_bull = h1_bull

    def get_klines(self, symbol, interval, limit=None):
        if interval == "1h":
            return _trend_candles(bull=self.h1_bull)
        return _trend_candles(bull=True)


def test_btc_align_blocks_opposing_1h_trend():
    # BTC 15m vote is bullish but its 1h trend is BEARISH -> block.
    ok, note = btc_align(_BTCMarket(h1_bull=False), "BULLISH")
    assert ok is False and "opposes" in note, note
    ok, note = btc_align(_BTCMarket(h1_bull=True), "BULLISH")
    assert ok is True, note


def test_klines_drop_unclosed_last_candle():
    from market import BinanceFutures
    m = BinanceFutures(api_key="", api_secret="", testnet=True)
    now = int(time.time() * 1000)

    def row(open_t, close_t, price):
        return [open_t, "100.0", "100.0", "100.0", str(price), "10",
                close_t, "10"]

    # last candle still forming (close_time in the future) -> dropped
    raw = [row(now - 900_000, now - 1, 100.0),
           row(now, now + 900_000, 101.0)]
    m._get = lambda path, params=None, signed=False: raw
    out = m.get_klines("TESTUSDT", "15m")
    assert len(out) == 1 and out[0]["close"] == 100.0

    # all candles closed -> nothing dropped
    raw2 = [row(now - 900_000, now - 1, 100.0)]
    m._get = lambda path, params=None, signed=False: raw2
    out2 = m.get_klines("TESTUSDT", "15m")
    assert len(out2) == 1 and out2[0]["close"] == 100.0


def test_conditional_orders_route_to_algo_api():
    """Regression (live testnet 2026-10-21): STOP_MARKET via
    /fapi/v1/order is rejected with -4120 since Binance migrated
    conditional orders to the Algo Order API. place_order must route
    STOP_MARKET/TAKE_PROFIT_MARKET to /fapi/v1/algoOrder with
    triggerPrice + newClientAlgoId, while LIMIT stays classic."""
    from market import BinanceFutures
    m = BinanceFutures(api_key="", api_secret="", testnet=True)
    calls = []
    m._post = lambda path, params=None: calls.append((path, params)) or {"ok": 1}

    m.place_order("BNBUSDT", "SELL", 1.5, order_type="STOP_MARKET",
                  stop_price=700.0, reduce_only=True, client_id="xsl")
    path, params = calls[-1]
    assert path == "/fapi/v1/algoOrder", path
    assert params["algoType"] == "CONDITIONAL"
    assert params["type"] == "STOP_MARKET"
    assert params["triggerPrice"] == 700.0
    assert params["newClientAlgoId"] == "xsl"
    assert params["reduceOnly"] == "true"
    assert "stopPrice" not in params and "newClientOrderId" not in params

    m.place_order("BNBUSDT", "SELL", 1.5, order_type="TAKE_PROFIT_MARKET",
                  stop_price=800.0, reduce_only=True, client_id="xtp")
    path, params = calls[-1]
    assert path == "/fapi/v1/algoOrder" and params["type"] == "TAKE_PROFIT_MARKET"

    m.place_order("BNBUSDT", "BUY", 1.5, order_type="LIMIT", price=750.0,
                  client_id="xentry")
    path, params = calls[-1]
    assert path == "/fapi/v1/order", path
    assert params["price"] == 750.0 and params["timeInForce"] == "GTC"
    assert params["newClientOrderId"] == "xentry"


def test_equity_usdt_uses_margin_balance_not_available():
    """Regression (live testnet evidence 2026-10-21): the daily loss
    cap read availableBalance via balance_usdt(), so margin reserved
    by two pending LIMIT entries (4878.87 -> 3296.40 available) read
    as a 32% 'loss' and tripped the 10% cap. equity_usdt must use
    totalMarginBalance, which margin reservation does not move."""
    from market import BinanceFutures
    m = BinanceFutures(api_key="", api_secret="", testnet=True)

    def fake_get(path, params=None, signed=False):
        if path == "/fapi/v2/account":
            return {"totalMarginBalance": "4873.50"}
        raise AssertionError(f"unexpected path {path}")

    m._get = fake_get
    assert abs(m.equity_usdt() - 4873.50) < 1e-9


def test_equity_usdt_fallback_wallet_plus_upnl():
    from market import BinanceFutures
    m = BinanceFutures(api_key="", api_secret="", testnet=True)

    def fake_get(path, params=None, signed=False):
        if path == "/fapi/v2/account":
            raise RuntimeError("boom")
        if path == "/fapi/v2/balance":
            return [{"asset": "USDT", "balance": "4800.0",
                     "availableBalance": "3200.0"}]
        if path == "/fapi/v2/positionRisk":
            return [{"symbol": "BTCUSDT", "positionAmt": "-0.006",
                     "entryPrice": "82198.5", "unRealizedProfit": "-5.25",
                     "leverage": "5"}]
        raise AssertionError(f"unexpected path {path}")

    m._get = fake_get
    # wallet 4800 + uPnL -5.25 = 4794.75 — NOT the 3200 available balance
    assert abs(m.equity_usdt() - 4794.75) < 1e-9


if __name__ == "__main__":
    test_mtf_trend_legs_reject_in_chop()
    test_mtf_trend_legs_pass_when_all_aligned()
    test_mtf_trend_legs_partial_support()
    test_mtf_hard_gate_rejects_when_1h_opposes()
    test_quality_score_discriminates()
    test_regime_brake_blocks_ranging()
    test_full_path_passes_when_trending()
    test_btc_align_blocks_opposing_1h_trend()
    test_klines_drop_unclosed_last_candle()
    test_conditional_orders_route_to_algo_api()
    test_equity_usdt_uses_margin_balance_not_available()
    test_equity_usdt_fallback_wallet_plus_upnl()
    print("engine fixes ok")
