"""Spot swing engine tests — structure, patterns, spot MTF, long-only.

No network: fake spot market + crafted candles.
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
import spot_engine
from spot_engine import (detect_patterns, evaluate_spot_symbol,
                         market_structure, spot_mtf_check)


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


def _zigzag_candles(n=120, rising=True):
    """4-step cycle (+3,+3,-1,-1) so swing points are true local
    extrema within a +-2 window and trend upward (or mirrored down)."""
    out = []
    p = 100.0
    steps = (3.0, 3.0, -1.0, -1.0) if rising else (-3.0, -3.0, 1.0, 1.0)
    for i in range(n):
        prev = p
        p += steps[i % 4]
        out.append({"open": prev, "high": max(prev, p) + 0.3,
                    "low": min(prev, p) - 0.3, "close": p,
                    "volume": 1000})
    return out


def _candle(o, h, l, c):
    return {"open": o, "high": h, "low": l, "close": c, "volume": 1000}


def test_structure_hh_hl_and_ll_lh():
    assert market_structure(_zigzag_candles(rising=True)) == "HH/HL"
    assert market_structure(_zigzag_candles(rising=False)) == "LL/LH"
    assert market_structure(_flat_candles()) == "MIXED"
    assert market_structure(_zigzag_candles(n=4)) == "UNKNOWN"


def test_patterns():
    prev = _candle(10, 10.5, 9, 9)
    cur = _candle(8.5, 11, 8.5, 10.5)
    assert "bullish_engulfing" in detect_patterns([prev, cur])
    prev_b = _candle(9, 10.5, 9, 10)
    cur_b = _candle(10.5, 10.6, 8.5, 9)
    assert "bearish_engulfing" in detect_patterns([prev_b, cur_b])
    hammer = _candle(10, 10.3, 8.0, 10.2)
    assert "hammer" in detect_patterns([prev, hammer])
    star = _candle(10, 12.0, 9.9, 10.2)
    assert "shooting_star" in detect_patterns([prev, star])


class _SpotMTFMarket:
    def __init__(self, trends=None):
        self.trends = trends or {}

    def get_klines(self, symbol, interval, limit=None):
        t = self.trends.get(interval, "bull")
        if t == "flat":
            return _flat_candles()
        return _trend_candles(bull=(t == "bull"))


def _spot_mtf(trends=None):
    return spot_mtf_check(_SpotMTFMarket(trends), "TESTUSDT", "BULLISH",
                          {"signal": "BULLISH WATCH"})


def test_spot_mtf_weights_and_hard_gate():
    ok, note, score = _spot_mtf({})
    assert ok is True and abs(score - 1.0) < 1e-9, note
    # 4h hard gate: bear 4h fails even with everything else aligned
    ok, note, score = _spot_mtf({"4h": "bear"})
    assert ok is False and "hard-gate" in note
    # 30m + 15m flat: 0.40 + 0.25 = 0.65 < 0.70 -> fail
    ok, note, score = _spot_mtf({"30m": "flat", "15m": "flat"})
    assert ok is False and abs(score - 0.65) < 1e-9, note
    # 30m flat only: 0.40 + 0.25 + 0.15 = 0.80 -> pass
    ok, note, score = _spot_mtf({"30m": "flat"})
    assert ok is True and abs(score - 0.80) < 1e-9, note


def _prepared_a1h():
    return {"signal": "BULLISH WATCH", "strength": 4,
            "regime": "TRENDING BULLISH", "price": 100.0, "atr": 1.0,
            "rsi": 55.0, "adx": 25.0, "volume_ratio": 1.2,
            "efficiency_ratio": 0.3, "plus_di": 25.0, "minus_di": 15.0,
            "reasons": ["test"]}


class _EvalSpotMarket:
    def __init__(self, rising=True):
        self.rising = rising

    def get_24h_quote_volume(self, symbol):
        return 50_000_000.0

    def get_klines(self, symbol, interval, limit=None):
        if symbol == "BTCUSDT":
            return _trend_candles(bull=True)
        if interval == "1h":
            return _zigzag_candles(rising=self.rising)
        return _trend_candles(bull=True)

    def get_ticker_price(self, symbol):
        return 100.0


def test_spot_long_only_and_structure_gate():
    orig = spot_engine.analyze_candles
    spot_engine.analyze_candles = (
        lambda sym, iv, candles: _prepared_a1h())
    try:
        sig, note = evaluate_spot_symbol("TESTUSDT", _EvalSpotMarket(rising=True))
        assert sig is not None, note
        assert sig["direction"] == "LONG"
        assert sig["stop_loss"] is None  # spot swing: no SL by design
        assert sig["tp1"] > sig["entry"] and sig["tp2"] > sig["tp1"]
        assert sig["structure"] == "HH/HL"
        sig2, note2 = evaluate_spot_symbol("TESTUSDT", _EvalSpotMarket(rising=False))
        assert sig2 is None and "structure" in note2, note2
    finally:
        spot_engine.analyze_candles = orig


def test_spot_rejects_bearish_trigger():
    orig = spot_engine.analyze_candles
    bearish = dict(_prepared_a1h(), signal="BEARISH WATCH")
    spot_engine.analyze_candles = (lambda sym, iv, candles: bearish)
    try:
        sig, note = evaluate_spot_symbol("TESTUSDT", _EvalSpotMarket(rising=True))
        assert sig is None and "long-only" in note, note
    finally:
        spot_engine.analyze_candles = orig
