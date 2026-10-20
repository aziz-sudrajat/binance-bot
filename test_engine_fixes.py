"""Engine fix tests (bug-hunt round 2) — no network.

Covers: MTF weighted score excludes the signal's own 15m leg (it can
now actually reject), and market.get_klines drops the still-forming
last candle so analysis never repaints.
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import signal_engine
from signal_engine import mtf_check


def _trend_candles(n=120, bull=True):
    out = []
    p = 100.0
    for i in range(n):
        p *= 1.002 if bull else 0.998
        o = p / 1.001
        out.append({"open": o, "high": p * 1.001, "low": o * 0.999,
                    "close": p, "volume": 1000 + i})
    return out


class _MTFMarket:
    def get_klines(self, symbol, interval, limit=None):
        return _trend_candles(bull=True)  # 4h/1h legs: BULLISH


def _mtf_with(five_m_signal, a30_signal):
    orig = signal_engine.analyze_candles
    signal_engine.analyze_candles = (
        lambda sym, iv, candles: {"signal": five_m_signal})
    try:
        return mtf_check(_MTFMarket(), "TESTUSDT", "BULLISH",
                         {"signal": "BULLISH WATCH"}, {"signal": a30_signal})
    finally:
        signal_engine.analyze_candles = orig


def test_mtf_weighted_rejects_when_soft_legs_oppose():
    """Regression for review T-1: hard gate 4h+1h aligned but 30m and 5m
    neutral. Old scoring added the always-aligned 15m leg (0.55+0.15 =
    0.70 = threshold) and could never reject. Excluding 15m and
    renormalising gives 0.55/0.85 = 0.65 < 0.70 -> must FAIL."""
    ok, note = _mtf_with("NEUTRAL", "NEUTRAL")
    assert ok is False, note
    assert "MTF score" in note


def test_mtf_weighted_passes_when_all_aligned():
    ok, note = _mtf_with("BULLISH WATCH", "BULLISH WATCH")
    assert ok is True, note


def test_mtf_weighted_partial_soft_support():
    # 30m aligned, 5m neutral: (0.30+0.25+0.20)/0.85 = 0.88 -> pass
    ok, note = _mtf_with("NEUTRAL", "BULLISH WATCH")
    assert ok is True, note
    # 30m neutral, 5m aligned: (0.30+0.25+0.10)/0.85 = 0.76 -> pass
    ok, note = _mtf_with("BULLISH WATCH", "NEUTRAL")
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


if __name__ == "__main__":
    test_mtf_weighted_rejects_when_soft_legs_oppose()
    test_mtf_weighted_passes_when_all_aligned()
    test_mtf_weighted_partial_soft_support()
    test_klines_drop_unclosed_last_candle()
    print("engine fixes ok")
