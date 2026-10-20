"""Smoke tests for signal engine (no network)."""

from signal_engine import analyze_candles, noise_ok, quality_score, build_trade_plan


def _trend_candles(n=120, bull=True):
    out = []
    p = 100.0
    for i in range(n):
        if bull:
            p *= 1.002
        else:
            p *= 0.998
        o = p / 1.001
        out.append({
            "open": o, "high": p * 1.001, "low": o * 0.999,
            "close": p, "volume": 1000 + i,
        })
    return out


def test_analyze_bull():
    a = analyze_candles("TESTUSDT", "15m", _trend_candles(bull=True))
    assert a["signal"] in ("BULLISH WATCH", "NEUTRAL", "BEARISH WATCH")
    assert "rsi" in a and "adx" in a


def test_analyze_bear_plan():
    a = analyze_candles("TESTUSDT", "15m", _trend_candles(bull=False))
    if a["signal"] == "BEARISH WATCH":
        plan = a["trade_plan"]
        assert plan["stop_loss"] > plan["entry"]
        assert plan["tp1"] < plan["entry"]


def test_noise_and_quality():
    a = analyze_candles("TESTUSDT", "15m", _trend_candles(bull=True))
    ok, note = noise_ok(a, "BULLISH")
    assert isinstance(ok, bool)
    q = quality_score(a, "BULLISH", 0.90)
    assert 5 <= q <= 98


def test_long_short_plans():
    cs = _trend_candles(bull=True)
    a = analyze_candles("X", "15m", cs)
    if a["trade_plan"] and a["signal"] == "BULLISH WATCH":
        p = a["trade_plan"]
        assert p["stop_loss"] < p["entry"] < p["tp1"]
    cs2 = _trend_candles(bull=False)
    a2 = analyze_candles("X", "15m", cs2)
    if a2["trade_plan"] and a2["signal"] == "BEARISH WATCH":
        p = a2["trade_plan"]
        assert p["tp1"] < p["entry"] < p["stop_loss"]


def _flat_candles(n=120, p=100.0):
    out = []
    for _ in range(n):
        out.append({"open": p, "high": p * 1.0005, "low": p * 0.9995,
                    "close": p, "volume": 1000})
    return out


def _volatile_candles(n=120):
    import random
    rnd = random.Random(42)
    out, p = [], 100.0
    for _ in range(n):
        chg = rnd.uniform(-0.02, 0.02)
        o = p
        p = p * (1 + chg)
        out.append({"open": o, "high": max(o, p) * 1.002,
                    "low": min(o, p) * 0.998, "close": p,
                    "volume": 500 + rnd.uniform(0, 2000)})
    return out


def test_trade_plan_invariants():
    """Entry/stop/TP ordering must hold for every market shape."""
    for cs in (_trend_candles(bull=True), _trend_candles(bull=False),
               _flat_candles(), _volatile_candles()):
        a = analyze_candles("X", "15m", cs)
        plan = a["trade_plan"]
        if a["signal"] == "BULLISH WATCH":
            assert plan["stop_loss"] < plan["entry"] < plan["tp1"], plan
            assert plan["tp1"] < plan["tp2"] < plan["tp3"], plan
        elif a["signal"] == "BEARISH WATCH":
            assert plan["tp1"] < plan["entry"] < plan["stop_loss"], plan
            assert plan["tp3"] < plan["tp2"] < plan["tp1"], plan
        if plan:
            assert plan["risk"] > 0
            assert plan["entry_low"] <= plan["entry"] <= plan["entry_high"]


if __name__ == "__main__":
    test_analyze_bull()
    test_analyze_bear_plan()
    test_noise_and_quality()
    test_long_short_plans()
    test_trade_plan_invariants()
    print("smoke ok")
