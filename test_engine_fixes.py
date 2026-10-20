"""Engine fix tests (bug-hunt round 2) — no network.

Covers: MTF set 1h/30m/15m/5m (user config 2026-10-21) — hard gate on
1h, weighted score that can still reject when both soft legs oppose —
and market.get_klines drops the still-forming last candle so analysis
never repaints.
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
        return _trend_candles(bull=True)  # 1h leg: BULLISH


class _MTFMarketBear1h:
    def get_klines(self, symbol, interval, limit=None):
        return _trend_candles(bull=(interval != "1h"))  # 1h leg: BEARISH


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
    """MTF set 1h/30m/15m/5m: hard 1h aligned (0.40) and the 15m signal
    leg always aligned (0.20), but 30m and 5m both neutral -> score
    0.60 < 0.70 -> must FAIL (the gate can still reject)."""
    ok, note = _mtf_with("NEUTRAL", "NEUTRAL")
    assert ok is False, note
    assert "MTF score" in note


def test_mtf_weighted_passes_when_all_aligned():
    ok, note = _mtf_with("BULLISH WATCH", "BULLISH WATCH")
    assert ok is True, note


def test_mtf_weighted_partial_soft_support():
    # 30m aligned, 5m neutral: 0.40+0.25+0.20 = 0.85 -> pass
    ok, note = _mtf_with("NEUTRAL", "BULLISH WATCH")
    assert ok is True, note
    # 30m neutral, 5m aligned: 0.40+0.20+0.15 = 0.75 -> pass
    ok, note = _mtf_with("BULLISH WATCH", "NEUTRAL")
    assert ok is True, note


def test_mtf_hard_gate_rejects_when_1h_opposes():
    # 1h BEARISH against a BULLISH signal: hard gate fails even though
    # 30m/15m/5m are all aligned.
    orig = signal_engine.analyze_candles
    signal_engine.analyze_candles = (
        lambda sym, iv, candles: {"signal": "BULLISH WATCH"})
    try:
        ok, note = mtf_check(_MTFMarketBear1h(), "TESTUSDT", "BULLISH",
                             {"signal": "BULLISH WATCH"},
                             {"signal": "BULLISH WATCH"})
    finally:
        signal_engine.analyze_candles = orig
    assert ok is False, note
    assert "hard-gate" in note


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
    test_mtf_hard_gate_rejects_when_1h_opposes()
    test_klines_drop_unclosed_last_candle()
    print("engine fixes ok")
