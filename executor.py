"""Auto-trader — place LONG/SHORT entries + SL/TP on Binance Futures."""

from __future__ import annotations

import json
import logging
import math
import os
import time
from decimal import Decimal, ROUND_DOWN

import config
from market import BinanceFutures
from scanner import SignalStore, ensure_data_dir

log = logging.getLogger("executor")


def _round_step(value: float, step: float) -> float:
    if step <= 0:
        return value
    d = Decimal(str(value))
    s = Decimal(str(step))
    return float((d / s).to_integral_value(rounding=ROUND_DOWN) * s)


def _round_tick(value: float, tick: float) -> float:
    return _round_step(value, tick)


class Executor:
    def __init__(self, market: BinanceFutures | None = None,
                 store: SignalStore | None = None):
        self.market = market or BinanceFutures()
        self.store = store or SignalStore()
        ensure_data_dir()
        self.state_path = os.path.join(config.DATA_DIR, "executor_state.json")
        self.state = self._load_state()

    def _load_state(self) -> dict:
        try:
            with open(self.state_path, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {"opened": {}, "history": []}

    def _save_state(self) -> None:
        with open(self.state_path, "w", encoding="utf-8") as f:
            json.dump(self.state, f, indent=2)

    def can_trade(self) -> tuple[bool, str]:
        mode = config.TRADE_MODE
        if mode == "paper":
            return False, "paper mode — signals only"
        if mode == "live" and config.LIVE_CONFIRM != "YES":
            return False, "live mode requires LIVE_CONFIRM=YES"
        if not self.market.api_key or not self.market.api_secret:
            return False, "missing BINANCE_API_KEY / BINANCE_API_SECRET"
        return True, "ok"

    def position_qty(self, symbol: str, entry: float, stop: float,
                     equity: float) -> float:
        """Risk-based size: risk_pct of equity / |entry-stop|."""
        risk_frac = max(0.1, config.RISK_PCT) / 100.0
        risk_usd = equity * risk_frac
        stop_dist = abs(entry - stop)
        if stop_dist <= 0 or entry <= 0:
            return 0.0
        raw_qty = risk_usd / stop_dist
        # leverage does not change notional risk formula above
        filt = self.market.symbol_filters(symbol)
        qty = _round_step(raw_qty, filt["stepSize"])
        if qty < filt["minQty"]:
            return 0.0
        return qty

    def open_from_signal(self, signal: dict) -> dict | None:
        ok, why = self.can_trade()
        if not ok:
            log.info("skip trade: %s", why)
            return None

        symbol = signal["symbol"]
        direction = signal["direction"]  # LONG / SHORT
        age_s = (time.time() * 1000 - signal.get("timestamp", 0)) / 1000
        if age_s > config.SIGNAL_TTL_SEC:
            log.info("skip %s: signal age %.0fs > TTL", symbol, age_s)
            return None

        # one position per symbol
        open_pos = self.market.positions()
        if any(p["symbol"] == symbol for p in open_pos):
            log.info("skip %s: already in position", symbol)
            return None
        # guard: pending entry order from an earlier attempt (LIMIT may sit unfilled)
        try:
            pending = [o for o in self.market.open_orders(symbol)
                       if str(o.get("clientOrderId", "")).startswith("bb")]
        except Exception:  # noqa: BLE001
            pending = []
        if pending:
            log.info("skip %s: %d pending entry order(s)", symbol, len(pending))
            return None
        if len(open_pos) >= config.MAX_OPEN_POSITIONS:
            log.info("skip %s: max open positions %d", symbol, config.MAX_OPEN_POSITIONS)
            return None

        equity = self.market.balance_usdt()
        if equity <= 0:
            log.warning("no USDT balance")
            return None

        entry = float(signal["entry"])
        stop = float(signal["stop_loss"])
        tp1 = float(signal["tp1"])
        qty = self.position_qty(symbol, entry, stop, equity)
        if qty <= 0:
            log.info("skip %s: qty=0 (risk too small)", symbol)
            return None

        filt = self.market.symbol_filters(symbol)
        entry = _round_tick(entry, filt["tickSize"])
        stop = _round_tick(stop, filt["tickSize"])
        tp1 = _round_tick(tp1, filt["tickSize"])

        self.market.set_margin_type(symbol, config.MARGIN_TYPE)
        self.market.set_leverage(symbol, config.MAX_LEVERAGE)

        side = "BUY" if direction == "LONG" else "SELL"
        close_side = "SELL" if direction == "LONG" else "BUY"
        client_id = f"bb{int(time.time())}{symbol[:6]}"[:36]

        log.info("OPEN %s %s qty=%s entry~%s stop=%s tp1=%s",
                 direction, symbol, qty, entry, stop, tp1)

        try:
            if config.ORDER_TYPE == "LIMIT":
                order = self.market.place_order(
                    symbol, side, qty, order_type="LIMIT", price=entry,
                    client_id=client_id)
            else:
                order = self.market.place_order(
                    symbol, side, qty, order_type="MARKET",
                    client_id=client_id)
        except Exception as e:  # noqa: BLE001
            log.error("entry failed %s: %s", symbol, e)
            return None

        # protective SL + TP (reduce-only)
        try:
            self.market.place_order(
                symbol, close_side, qty, order_type="STOP_MARKET",
                stop_price=stop, reduce_only=True,
                client_id=(client_id + "sl")[:36])
        except Exception as e:  # noqa: BLE001
            log.error("SL failed %s: %s — manual risk!", symbol, e)

        try:
            self.market.place_order(
                symbol, close_side, qty, order_type="TAKE_PROFIT_MARKET",
                stop_price=tp1, reduce_only=True,
                client_id=(client_id + "tp")[:36])
        except Exception as e:  # noqa: BLE001
            log.warning("TP failed %s: %s", symbol, e)

        rec = {
            "symbol": symbol,
            "direction": direction,
            "qty": qty,
            "entry": entry,
            "stop": stop,
            "tp1": tp1,
            "order": order,
            "at": int(time.time() * 1000),
            "quality": signal.get("quality"),
        }
        self.state.setdefault("opened", {})[symbol] = rec
        self.state.setdefault("history", []).append(rec)
        self._save_state()
        # mark signal consumed so the next trade/loop run doesn't re-open it
        sid = signal.get("_sid")
        if sid:
            try:
                self.store.mark(sid, "opened")
            except Exception as e:  # noqa: BLE001
                log.warning("mark opened %s: %s", sid, e)
        return rec

    def process_fresh_signals(self) -> list[dict]:
        ok, why = self.can_trade()
        if not ok:
            log.info("executor idle: %s", why)
            return []
        results = []
        for sig in self.store.fresh(limit=10):
            rec = self.open_from_signal(sig)
            if rec:
                results.append(rec)
        return results
