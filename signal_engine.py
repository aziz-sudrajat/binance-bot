"""Signal engine — bidirectional LONG + SHORT for Binance USDT-M futures.

Pipeline per symbol:
  1. Liquidity (24h volume)
  2. 15m setup (EMA / RSI / MACD / volume) → BULLISH or BEARISH WATCH
  3. Regime brake (RANGING = no-trade zone)
  4. Noise filter (efficiency ratio, doji, flips, DI conflict)
  5. MTF weighted (hard 1h, trend-based legs 1h/30m/15m/5m)
  6. Optional BTC alignment (15m direction + 1h trend context)
  7. Freshness vs entry zone
  8. Quality score ≥ MIN_QUALITY
"""

from __future__ import annotations

import math
import time
from typing import Any

import config
from indicators import (
    adx, atr_wilder, average, candle_noise, efficiency_ratio,
    ema_series, macd, rsi_wilder,
)


def _finite(v) -> bool:
    if v is None:
        return False
    try:
        return math.isfinite(float(v))
    except (TypeError, ValueError):
        return False


def _dir(sig: str) -> str:
    s = str(sig or "")
    if s.startswith("BULL"):
        return "BULLISH"
    if s.startswith("BEAR"):
        return "BEARISH"
    return "NEUTRAL"


# ------------------------------------------------------------------ regime
def classify_regime(price, e9, e21, e50, atr, adx_val) -> str:
    price, atr, adx_val = float(price), float(atr or 0), float(adx_val or 0)
    atr_pct = atr / price * 100 if price else 0
    aligned_bull = e9 > e21 > e50
    aligned_bear = e9 < e21 < e50
    if atr_pct >= 3.5:
        return "HIGH VOLATILITY"
    if adx_val >= 23 and aligned_bull:
        return "TRENDING BULLISH"
    if adx_val >= 23 and aligned_bear:
        return "TRENDING BEARISH"
    if adx_val < 18:
        return "RANGING"
    return "MIXED"


# -------------------------------------------------------------- trade plan
def build_trade_plan(signal: str, price: float, ema9: float, ema21: float,
                     atr: float, candles: list) -> dict:
    price, ema9, ema21 = float(price), float(ema9), float(ema21)
    atr = float(atr) if _finite(atr) and atr > 0 else max(price * 0.005, 1e-8)
    recent = candles[-config.SWING_LOOKBACK:]
    swing_low = min(float(c["low"]) for c in recent) if recent else price - atr
    swing_high = max(float(c["high"]) for c in recent) if recent else price + atr

    if signal == "BULLISH WATCH":
        pullback = min(price, max(ema9, ema21))
        entry = max(price - config.ENTRY_MAX_PULLBACK_ATR * atr, pullback)
        stop = min(entry - config.STOP_ATR_MIN * atr, swing_low - 0.10 * atr)
        stop = max(stop, entry - config.STOP_ATR_MAX * atr)
        direction = 1
    else:
        pullback = max(price, min(ema9, ema21))
        entry = min(price + config.ENTRY_MAX_PULLBACK_ATR * atr, pullback)
        stop = max(entry + config.STOP_ATR_MIN * atr, swing_high + 0.10 * atr)
        stop = min(stop, entry + config.STOP_ATR_MAX * atr)
        direction = -1

    risk = max(abs(entry - stop), atr * 0.5)
    zone = config.ENTRY_ZONE_ATR * atr
    return {
        "entry": entry,
        "entry_low": entry - zone,
        "entry_high": entry + zone,
        "stop_loss": stop,
        "tp1": entry + direction * risk * config.TP1_RR,
        "tp2": entry + direction * risk * config.TP2_RR,
        "tp3": entry + direction * risk * config.TP3_RR,
        "rr1": config.TP1_RR,
        "risk": risk,
        "atr": atr,
    }


# ---------------------------------------------------------------- analysis
def analyze_candles(symbol: str, interval: str, candles: list) -> dict:
    if not isinstance(candles, list) or len(candles) < config.MIN_CANDLES:
        raise ValueError(f"not enough candles: {len(candles) if candles else 0}")

    opens = [float(c["open"]) for c in candles]
    closes = [float(c["close"]) for c in candles]
    volumes = [float(c["volume"]) for c in candles]

    e9a = ema_series(closes, 9)
    e21a = ema_series(closes, 21)
    e50a = ema_series(closes, 50)
    m = macd(closes)
    rsi = rsi_wilder(closes, 14)
    atr = atr_wilder(candles, config.ATR_PERIOD)
    adx_pack = adx(candles, 14)
    noise = candle_noise(candles, 5)
    er = efficiency_ratio(closes, 10)

    i = len(closes) - 1
    price, open_ = closes[i], opens[i]
    e9, e21, e50 = e9a[i], e21a[i], e50a[i]
    avg_vol20 = average(volumes[max(0, i - 20): i])
    volume_ratio = volumes[i] / avg_vol20 if avg_vol20 > 0 else 1.0

    bull = bear = 0
    rb, rs = [], []
    if e9 > e21:
        bull += 1; rb.append("EMA9>EMA21")
    else:
        bear += 1; rs.append("EMA9<EMA21")
    if e21 > e50:
        bull += 1; rb.append("EMA21>EMA50")
    else:
        bear += 1; rs.append("EMA21<EMA50")
    if price > e21:
        bull += 1; rb.append("price>EMA21")
    else:
        bear += 1; rs.append("price<EMA21")
    if e21 > e21a[i - 1]:
        bull += 1; rb.append("EMA21 rising")
    elif e21 < e21a[i - 1]:
        bear += 1; rs.append("EMA21 falling")
    if 52 <= rsi <= 72:
        bull += 1; rb.append("RSI bull")
    if 28 <= rsi <= 48:
        bear += 1; rs.append("RSI bear")
    if m["histogram"] > 0:
        bull += 1; rb.append("MACD+")
    elif m["histogram"] < 0:
        bear += 1; rs.append("MACD-")
    if price > open_:
        bull += 1; rb.append("green")
    elif price < open_:
        bear += 1; rs.append("red")
    if volume_ratio >= 1.15:
        if price >= open_:
            bull += 1; rb.append("vol+")
        else:
            bear += 1; rs.append("vol+")

    signal, score, reasons = "NEUTRAL", max(bull, bear), []
    if bull >= config.SIGNAL_MIN_SCORE and bull - bear >= 2:
        signal, score, reasons = "BULLISH WATCH", bull, rb
    elif bear >= config.SIGNAL_MIN_SCORE and bear - bull >= 2:
        signal, score, reasons = "BEARISH WATCH", bear, rs

    strength = 0 if signal == "NEUTRAL" else min(5, max(1, round(score / 1.5)))
    plan = build_trade_plan(signal, price, e9, e21, atr, candles) if signal != "NEUTRAL" else None
    regime = classify_regime(price, e9, e21, e50, atr, adx_pack["adx"])

    return {
        "symbol": symbol, "interval": interval, "signal": signal,
        "strength": strength, "regime": regime, "score": score,
        "bull_score": bull, "bear_score": bear, "price": price,
        "ema9": e9, "ema21": e21, "ema50": e50,
        "rsi": rsi, "macd_histogram": m["histogram"],
        "volume_ratio": volume_ratio, "atr": atr,
        "adx": adx_pack["adx"],
        "plus_di": adx_pack["plus_di"], "minus_di": adx_pack["minus_di"],
        "efficiency_ratio": er,
        "doji_ratio": noise["doji_ratio"],
        "direction_flips": noise["flips"],
        "reasons": reasons, "trade_plan": plan,
    }


def mtf_trend(closes: list) -> str:
    if len(closes) < 55:
        return "NEUTRAL"
    e21a = ema_series(closes, 21)
    e50a = ema_series(closes, 50)
    i = len(closes) - 1
    price = float(closes[i])
    if price > e21a[i] > e50a[i] and e21a[i] >= e21a[i - 1]:
        return "BULLISH"
    if price < e21a[i] < e50a[i] and e21a[i] <= e21a[i - 1]:
        return "BEARISH"
    return "NEUTRAL"


def noise_ok(a15: dict, direction: str) -> tuple[bool, str]:
    er = float(a15.get("efficiency_ratio") or 0)
    if er < config.NOISE_ER_MIN:
        return False, f"noise: ER {er:.2f} < {config.NOISE_ER_MIN} (choppy)"
    doji = float(a15.get("doji_ratio") or 0)
    if doji > config.NOISE_DOJI_MAX:
        return False, f"noise: doji {doji:.0%} (indecision)"
    flips = int(a15.get("direction_flips") or 0)
    if flips > config.NOISE_FLIPS_MAX:
        return False, f"noise: {flips} flips (choppy tape)"
    adx_v = float(a15.get("adx") or 0)
    pdi = float(a15.get("plus_di") or 0)
    mdi = float(a15.get("minus_di") or 0)
    if adx_v >= config.NOISE_DI_ADX_MIN:
        if direction == "BULLISH" and mdi > pdi + 5:
            return False, f"noise: -DI>{pdi:.0f} against LONG"
        if direction == "BEARISH" and pdi > mdi + 5:
            return False, f"noise: +DI against SHORT"
    # overextension chase
    atr = float(a15.get("atr") or 0) or 1e-8
    dist = (float(a15["price"]) - float(a15["ema21"])) / atr
    if direction == "BULLISH" and dist > config.OVEREXTENSION_ATR_MAX:
        rsi = float(a15.get("rsi") or 50)
        if rsi >= 65:
            return False, f"noise: overextended +{dist:.1f}ATR RSI{rsi:.0f}"
    if direction == "BEARISH" and dist < -config.OVEREXTENSION_ATR_MAX:
        rsi = float(a15.get("rsi") or 50)
        if rsi <= 35:
            return False, f"noise: overextended {dist:.1f}ATR RSI{rsi:.0f}"
    return True, "noise clean"


def mtf_check(market, symbol: str, direction: str,
              a15: dict) -> tuple[bool, str, float]:
    """MTF gate over the user's set 1h/30m/15m/5m. Returns
    (ok, note, weighted_score).

    Legs 1h/30m/5m are EMA-trend based (mtf_trend), NOT the setup
    voting used for the 15m trigger: the voting legs were perfectly
    correlated with the trigger itself, so the gate rejected nothing
    (0 rejects in ~160 evaluations, live testnet audit 2026-10-21).
    Trend legs measure whether the move is sustained on each TF.
    The 15m leg is the signal itself and stays for reporting/scoring
    as the user configured; it cannot carry the gate alone
    (1h 0.40 + 15m 0.20 = 0.60 < 0.70)."""
    want = direction
    try:
        k1h = market.get_klines(symbol, "1h")
        k30 = market.get_klines(symbol, "30m")
        k5 = market.get_klines(symbol, "5m")
    except Exception as e:  # noqa: BLE001
        return False, f"MTF data error: {e}", 0.0
    legs = {
        "1h": mtf_trend([float(c["close"]) for c in k1h]),
        "30m": mtf_trend([float(c["close"]) for c in k30]),
        "15m": _dir(a15.get("signal")),
        "5m": mtf_trend([float(c["close"]) for c in k5]),
    }
    detail = ", ".join(f"{tf}={d.lower()}" for tf, d in legs.items())
    for tf in config.MTF_HARD:
        if legs.get(tf) != want:
            return False, f"MTF hard-gate fail ({detail}; need {want.lower()} on {'/'.join(config.MTF_HARD)})", 0.0
    total_w = sum(config.MTF_WEIGHTS.get(tf, 0) for tf in legs) or 1.0
    score = sum(config.MTF_WEIGHTS.get(tf, 0) / total_w
                for tf, d in legs.items() if d == want)
    if score < config.MTF_MIN_SCORE:
        return False, f"MTF score {score:.2f} < {config.MTF_MIN_SCORE} ({detail})", score
    return True, f"MTF ok score {score:.2f} ({detail})", score


def btc_align(market, direction: str) -> tuple[bool, str]:
    try:
        a = analyze_candles("BTCUSDT", "15m", market.get_klines("BTCUSDT", "15m"))
    except Exception as e:  # noqa: BLE001
        return False, f"BTC data error: {e}"
    got = _dir(a.get("signal"))
    if got != direction:
        return False, f"BTC 15m {got.lower()} != signal {direction.lower()}"
    # Market context: BTC's 1h trend must not OPPOSE the trade. The old
    # check only matched the 15m vote sign — the same stacked momentum
    # vote as the trigger — so it waved through a full book of LONGs
    # while BTC's higher timeframe was rolling over (audit 2026-10-21).
    # NEUTRAL is allowed; only an opposing trend blocks.
    try:
        k1h = market.get_klines("BTCUSDT", "1h")
        t1h = mtf_trend([float(c["close"]) for c in k1h])
    except Exception as e:  # noqa: BLE001
        return False, f"BTC data error: {e}"
    if (direction == "BULLISH" and t1h == "BEARISH") or \
            (direction == "BEARISH" and t1h == "BULLISH"):
        return False, f"BTC 1h trend {t1h.lower()} opposes {direction.lower()}"
    return True, f"BTC aligned {direction.lower()} (1h {t1h.lower()})"


def quality_score(a15: dict, direction: str, mtf_score: float) -> int:
    """Discriminative quality (rebuilt in the 2026-10-21 audit).

    The old scale started strength 5 at base 90, so nearly every
    approval landed at 84-98 and MIN_QUALITY=62 never rejected
    anything. The new scale spreads 40-98: strength base is lower,
    regime swings are real (+8 / -12), and the MTF weighted score
    contributes up to 10 points instead of a flat +2 for passing."""
    base = {3: 45, 4: 60, 5: 75}.get(int(a15.get("strength") or 0), 40)
    # regime
    regime = a15.get("regime") or ""
    if direction == "BULLISH" and regime == "TRENDING BULLISH":
        base += 8
    elif direction == "BEARISH" and regime == "TRENDING BEARISH":
        base += 8
    elif regime == "HIGH VOLATILITY":
        base -= 10
    elif regime == "RANGING":
        base -= 12
    # volume
    vol = float(a15.get("volume_ratio") or 1)
    if vol >= 1.3:
        base += 5
    elif vol >= 1.15:
        base += 2
    # ER
    er = float(a15.get("efficiency_ratio") or 0)
    if er >= 0.35:
        base += 4
    elif er >= 0.25:
        base += 2
    # DI
    if direction == "BULLISH" and float(a15.get("plus_di") or 0) > float(a15.get("minus_di") or 0):
        base += 3
    if direction == "BEARISH" and float(a15.get("minus_di") or 0) > float(a15.get("plus_di") or 0):
        base += 3
    # MTF weighted score (0..1) contributes up to 10 points
    try:
        ms = max(0.0, min(1.0, float(mtf_score)))
    except (TypeError, ValueError):
        ms = 0.0
    base += int(round(10 * ms))
    return max(5, min(98, int(round(base))))


def freshness_ok(market, symbol: str, direction: str, plan: dict) -> tuple[float | None, str | None]:
    try:
        live = market.get_ticker_price(symbol)
    except Exception:  # noqa: BLE001
        return None, "freshness: live price unavailable"
    if not _finite(live) or live <= 0:
        return None, "freshness: live price invalid"
    if direction == "BEARISH":
        drift = ((plan["entry_low"] - live) / plan["entry_low"] * 100
                 if plan["entry_low"] > 0 else 0)
        if drift > config.FRESHNESS_MAX_DRIFT_PCT:
            return None, f"freshness: price -{drift:.2f}% below zone (stale short)"
    else:
        drift = (live - plan["entry_high"]) / plan["entry_high"] * 100
        if drift > config.FRESHNESS_MAX_DRIFT_PCT:
            return None, f"freshness: price +{drift:.2f}% above zone (stale long)"
    return live, None


def evaluate_symbol(symbol: str, market) -> tuple[dict | None, str]:
    """Full pipeline. Returns (signal_dict, note) or (None, reject_reason)."""
    symbol = symbol.upper()

    # 0 liquidity
    vol = market.get_24h_quote_volume(symbol)
    if vol is None or not _finite(vol):
        return None, "24h volume unavailable"
    if vol < config.MIN_24H_VOLUME_USD:
        return None, f"volume ${vol:,.0f} < ${config.MIN_24H_VOLUME_USD:,.0f}"

    # 1 15m setup
    try:
        k15 = market.get_klines(symbol, "15m")
        a15 = analyze_candles(symbol, "15m", k15)
    except Exception as e:  # noqa: BLE001
        return None, f"15m error: {e}"

    sig = str(a15["signal"])
    is_bull = sig.startswith("BULL")
    is_bear = sig.startswith("BEAR")
    if config.BULLISH_ONLY and not is_bull:
        return None, f"bullish-only: {sig}"
    if not is_bull and not is_bear:
        return None, f"no directional setup ({sig})"
    direction = "BULLISH" if is_bull else "BEARISH"
    if not a15.get("trade_plan"):
        return None, "no trade plan"
    if a15["strength"] < config.MIN_STRENGTH:
        return None, f"strength {a15['strength']} < {config.MIN_STRENGTH}"

    # 2b regime brake: trend engine does not trade a ranging symbol
    if config.REGIME_BLOCK_RANGING and a15.get("regime") == "RANGING":
        return None, "regime: RANGING (ADX low, choppy) — no-trade zone"

    # 3 noise
    ok, note = noise_ok(a15, direction)
    if not ok:
        return None, note

    # 4 MTF (legs fetched inside mtf_check)
    ok, mtf_note, mtf_score = mtf_check(market, symbol, direction, a15)
    if not ok:
        return None, mtf_note

    # 5 BTC
    if config.BTC_ALIGN and symbol != "BTCUSDT":
        ok, btc_note = btc_align(market, direction)
        if not ok:
            return None, btc_note
    else:
        btc_note = "BTC n/a"

    # 6 freshness
    live, frej = freshness_ok(market, symbol, direction, a15["trade_plan"])
    if frej:
        return None, frej

    # 7 quality
    q = quality_score(a15, direction, mtf_score)
    if q < config.MIN_QUALITY:
        return None, f"quality {q} < {config.MIN_QUALITY}"

    plan = a15["trade_plan"]
    side = "LONG" if direction == "BULLISH" else "SHORT"
    signal = {
        "symbol": symbol,
        "direction": side,
        "interval": "15m",
        "entry": plan["entry"],
        "entry_low": plan["entry_low"],
        "entry_high": plan["entry_high"],
        "stop_loss": plan["stop_loss"],
        "tp1": plan["tp1"],
        "tp2": plan["tp2"],
        "tp3": plan["tp3"],
        "rr1": plan["rr1"],
        "quality": q,
        "strength": a15["strength"],
        "regime": a15["regime"],
        "rsi": round(a15["rsi"], 1),
        "adx": round(float(a15["adx"]), 1),
        "volume_ratio": round(float(a15["volume_ratio"]), 2),
        "efficiency_ratio": round(float(a15["efficiency_ratio"]), 3),
        "atr": plan["atr"],
        "price": a15["price"],
        "live_price": live,
        "reasons": a15["reasons"],
        "mtf": mtf_note,
        "btc": btc_note,
        "timestamp": int(time.time() * 1000),
    }
    note = (f"PASS {side} Q={q} strength={a15['strength']} "
            f"regime={a15['regime']}")
    return signal, note
