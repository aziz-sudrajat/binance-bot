"""Spot swing engine — LONG-only, no stop-loss.

Pipeline per symbol (spot = owning the asset, so there is no
liquidation and no SL: a position is held until the bot-side TP or a
manual close):
  0. Liquidity (24h quote volume)
  1. 1h trigger (EMA / RSI / MACD / volume votes, reused from the
     futures engine) -> must be BULLISH WATCH (spot is LONG-only)
  2. Market-structure gate: HH/HL (higher highs + higher lows)
  3. MTF: hard 4h trend gate, weighted score over 4h/1h/30m/15m
  4. BTC 4h trend context (must not oppose)
  5. Quality score >= SPOT_MIN_QUALITY (pattern adds a small bonus)
  6. Trade plan: MARKET entry, TP1/TP2 by ATR multiples, NO stop-loss

Freshness is intentionally skipped: spot entries are MARKET buys, so
there is no LIMIT zone to drift from.
"""

from __future__ import annotations

import logging
import math
import time
from collections import Counter
from typing import Any

import config
from scanner import SignalStore
from signal_engine import _dir, analyze_candles, mtf_trend, quality_score

log = logging.getLogger("spot_engine")


# ------------------------------------------------------------ market structure
def _swings(candles: list) -> tuple[list, list]:
    """Swing highs/lows: a candle extreme vs 2 candles each side."""
    highs = [float(c["high"]) for c in candles]
    lows = [float(c["low"]) for c in candles]
    sh, sl = [], []
    for i in range(2, len(candles) - 2):
        if highs[i] == max(highs[i - 2:i + 3]):
            sh.append(highs[i])
        if lows[i] == min(lows[i - 2:i + 3]):
            sl.append(lows[i])
    return sh, sl


def market_structure(candles: list) -> str:
    """HH/HL, LL/LH, or MIXED from the last two swing points."""
    sh, sl = _swings(candles)
    if len(sh) < 2 or len(sl) < 2:
        return "UNKNOWN"
    if sh[-1] > sh[-2] and sl[-1] > sl[-2]:
        return "HH/HL"
    if sh[-1] < sh[-2] and sl[-1] < sl[-2]:
        return "LL/LH"
    return "MIXED"


# ------------------------------------------------------------------- patterns
def _body(c: dict) -> float:
    return abs(float(c["close"]) - float(c["open"]))


def detect_patterns(candles: list) -> list[str]:
    """Patterns on the last CLOSED candle (+ engulfing vs the prior one).

    Bullish: engulfing, hammer. Bearish: engulfing, shooting star.
    Only the direction-relevant ones matter to the LONG-only engine,
    but all are detected for the log/reasons."""
    if len(candles) < 2:
        return []
    prev, cur = candles[-2], candles[-1]
    out = []
    po, pc = float(prev["open"]), float(prev["close"])
    co, cc = float(cur["open"]), float(cur["close"])
    # engulfing (body fully covers the prior body)
    if pc < po and cc > co and co <= pc and cc >= po:
        out.append("bullish_engulfing")
    if pc > po and cc < co and co >= pc and cc <= po:
        out.append("bearish_engulfing")
    # pin bars on the last candle
    body = _body(cur)
    upper = float(cur["high"]) - max(co, cc)
    lower = min(co, cc) - float(cur["low"])
    if body > 0 and lower >= 2 * body and upper <= body:
        out.append("hammer")
    if body > 0 and upper >= 2 * body and lower <= body:
        out.append("shooting_star")
    return out


# ------------------------------------------------------------------------ MTF
def spot_mtf_check(market, symbol: str, direction: str,
                   a1h: dict) -> tuple[bool, str, float]:
    """MTF gate over 4h/1h/30m/15m. 4h is the hard trend gate; the 1h
    leg is the trigger itself; 30m/15m are EMA-trend legs (same
    trend-leg lesson as the futures audit: voting legs correlate with
    the trigger and reject nothing)."""
    want = direction
    try:
        k4h = market.get_klines(symbol, "4h")
        k30 = market.get_klines(symbol, "30m")
        k15 = market.get_klines(symbol, "15m")
    except Exception as e:  # noqa: BLE001
        return False, f"spot MTF data error: {e}", 0.0
    legs = {
        "4h": mtf_trend([float(c["close"]) for c in k4h]),
        "1h": _dir(a1h.get("signal")),
        "30m": mtf_trend([float(c["close"]) for c in k30]),
        "15m": mtf_trend([float(c["close"]) for c in k15]),
    }
    detail = ", ".join(f"{tf}={d.lower()}" for tf, d in legs.items())
    if legs.get("4h") != want:
        return False, f"spot MTF hard-gate fail ({detail}; need {want.lower()} on 4h)", 0.0
    total_w = sum(config.SPOT_MTF_WEIGHTS.get(tf, 0) for tf in legs) or 1.0
    score = sum(config.SPOT_MTF_WEIGHTS.get(tf, 0) / total_w
                for tf, d in legs.items() if d == want)
    if score < config.SPOT_MTF_MIN_SCORE:
        return False, f"spot MTF score {score:.2f} < {config.SPOT_MTF_MIN_SCORE} ({detail})", score
    return True, f"spot MTF ok score {score:.2f} ({detail})", score


def spot_btc_check(market, direction: str) -> tuple[bool, str]:
    """BTC 4h trend must not oppose the trade (NEUTRAL passes)."""
    try:
        k4h = market.get_klines("BTCUSDT", "4h")
        t4h = mtf_trend([float(c["close"]) for c in k4h])
    except Exception as e:  # noqa: BLE001
        return False, f"spot BTC data error: {e}"
    if (direction == "BULLISH" and t4h == "BEARISH") or \
            (direction == "BEARISH" and t4h == "BULLISH"):
        return False, f"spot BTC 4h trend {t4h.lower()} opposes {direction.lower()}"
    return True, f"spot BTC aligned {direction.lower()} (4h {t4h.lower()})"


# ------------------------------------------------------------------- evaluate
def evaluate_spot_symbol(symbol: str, market) -> tuple[dict | None, str]:
    """Full spot pipeline. Returns (signal_dict, note) or (None, reason)."""
    symbol = symbol.upper()

    # 0 liquidity
    vol = market.get_24h_quote_volume(symbol)
    try:
        vol_ok = vol is not None and math.isfinite(float(vol))
    except (TypeError, ValueError):
        vol_ok = False
    if not vol_ok:
        return None, "spot 24h volume unavailable"
    if vol < config.MIN_24H_VOLUME_USD:
        return None, f"spot volume ${vol:,.0f} < ${config.MIN_24H_VOLUME_USD:,.0f}"

    # 1 1h trigger (LONG-only: spot cannot short)
    try:
        k1h = market.get_klines(symbol, "1h")
        a1h = analyze_candles(symbol, "1h", k1h)
    except Exception as e:  # noqa: BLE001
        return None, f"spot 1h error: {e}"
    sig = str(a1h["signal"])
    if not sig.startswith("BULL"):
        return None, f"spot long-only: {sig}"
    if a1h["strength"] < config.MIN_STRENGTH:
        return None, f"spot strength {a1h['strength']} < {config.MIN_STRENGTH}"

    # 2 market structure gate
    struct = market_structure(k1h)
    if struct != "HH/HL":
        return None, f"spot structure: {struct} (need HH/HL)"

    # 3 MTF
    ok, mtf_note, mtf_score = spot_mtf_check(market, symbol, "BULLISH", a1h)
    if not ok:
        return None, mtf_note

    # 4 BTC context
    if config.BTC_ALIGN and symbol != "BTCUSDT":
        ok, btc_note = spot_btc_check(market, "BULLISH")
        if not ok:
            return None, btc_note
    else:
        btc_note = "spot BTC n/a"

    # 5 quality (+ small bonus for a bullish pattern on the last candle)
    patterns = detect_patterns(k1h)
    q = quality_score(a1h, "BULLISH", mtf_score)
    if any(p in ("bullish_engulfing", "hammer") for p in patterns):
        q = min(98, q + 3)
    if q < config.SPOT_MIN_QUALITY:
        return None, f"spot quality {q} < {config.SPOT_MIN_QUALITY}"

    entry = float(a1h["price"])
    atr = float(a1h["atr"])
    signal = {
        "symbol": symbol,
        "direction": "LONG",
        "market": "spot",
        "interval": "1h",
        "entry": entry,
        "entry_low": entry,
        "entry_high": entry,
        "stop_loss": None,  # spot swing: no SL by design
        "tp1": entry + config.SPOT_TP1_ATR * atr,
        "tp2": entry + config.SPOT_TP2_ATR * atr,
        "tp1_atr": config.SPOT_TP1_ATR,
        "tp2_atr": config.SPOT_TP2_ATR,
        "quality": q,
        "strength": a1h["strength"],
        "regime": a1h["regime"],
        "rsi": round(a1h["rsi"], 1),
        "adx": round(float(a1h["adx"]), 1),
        "volume_ratio": round(float(a1h["volume_ratio"]), 2),
        "efficiency_ratio": round(float(a1h["efficiency_ratio"]), 3),
        "atr": atr,
        "price": entry,
        "live_price": entry,
        "reasons": a1h["reasons"],
        "structure": struct,
        "patterns": patterns,
        "mtf": mtf_note,
        "btc": btc_note,
        "timestamp": int(time.time() * 1000),
    }
    note = (f"SPOT PASS LONG Q={q} strength={a1h['strength']} "
            f"regime={a1h['regime']} structure={struct} patterns={patterns}")
    return signal, note


def _classify_reason(reason: str) -> str:
    r = (reason or "").lower()
    for key in ("volume", "1h", "structure", "mtf", "btc", "quality",
                "strength", "long-only"):
        if key in r:
            return key
    return "other"


def run_spot_scan(market=None, store: SignalStore | None = None) -> dict:
    """One spot scan pass over the top USDT spot pairs."""
    from spot_market import SpotClient

    market = market or SpotClient()
    own_store = store is None
    store = store or SignalStore(path=config.SPOT_SIGNALS_DB)
    t0 = time.time()
    symbols = market.top_symbols_by_volume()
    log.info("spot scanning %d symbols (mode=%s)", len(symbols), config.SPOT_MODE)

    approved = rejected = errors = 0
    funnel: Counter = Counter()
    signals: list[dict] = []
    for i, sym in enumerate(symbols):
        try:
            sig, note = evaluate_spot_symbol(sym, market)
            if sig is None:
                rejected += 1
                funnel[_classify_reason(note)] += 1
                log.debug("SPOT REJECT %s: %s", sym, note)
            else:
                store.upsert(sig)
                approved += 1
                signals.append(sig)
                log.info("SPOT APPROVED %s Q=%s — %s",
                         sym, sig["quality"], note)
        except Exception as e:  # noqa: BLE001
            errors += 1
            funnel["error"] += 1
            log.warning("SPOT ERROR %s: %s", sym, e)
        if (i + 1) % 20 == 0:
            log.info("spot progress %d/%d", i + 1, len(symbols))
        time.sleep(0.12)  # soft rate limit

    expired = store.expire_old()
    stats = {
        "at": int(time.time() * 1000),
        "symbols": len(symbols),
        "approved": approved,
        "rejected": rejected,
        "errors": errors,
        "expired": expired,
        "reject_funnel": dict(funnel.most_common()),
        "signals": signals,
        "elapsed_sec": round(time.time() - t0, 1),
    }
    log.info("spot scan done: approved=%d rejected=%d errors=%d funnel=%s",
             approved, rejected, errors, dict(funnel.most_common()))
    if own_store:
        store.close()
    return stats
