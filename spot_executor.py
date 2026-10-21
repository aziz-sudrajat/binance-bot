"""Spot auto-trader — buy USDT pairs, hold to TP, no stop-loss.

Safety model (spot differs fundamentally from futures):
- No leverage, no margin, no liquidation: the worst case is holding
  a dipped asset, not a forced close. Hence NO SL by design.
- Buys are MARKET with a fixed USDT amount (quoteOrderQty) so sizing
  never depends on stop distance (there is none).
- Exits are bot-side, checked every cycle: full MARKET sell when the
  live price reaches TP1. The position record is dropped if the asset
  was sold externally (base balance gone).
- State lives in spot_state.json, fully separate from the futures
  executor state, so the two engines never interfere.
"""

from __future__ import annotations

import json
import logging
import os
import time

import config
import settings
from scanner import SignalStore

log = logging.getLogger("spot_executor")


class SpotExecutor:
    def __init__(self, market=None, store: SignalStore | None = None):
        from spot_market import SpotClient

        self.market = market or SpotClient()
        self.store = store or SignalStore(path=config.SPOT_SIGNALS_DB)
        self.state_path = config.SPOT_STATE_FILE
        self.state = self._load_state()

    def _load_state(self) -> dict:
        try:
            with open(self.state_path, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {"held": {}, "history": []}

    def _save_state(self) -> None:
        os.makedirs(os.path.dirname(self.state_path) or ".", exist_ok=True)
        with open(self.state_path, "w", encoding="utf-8") as f:
            json.dump(self.state, f, indent=2)

    def close(self) -> None:
        self.store.close()

    # ------------------------------------------------------------ entries
    def buy_from_signal(self, signal: dict) -> dict | None:
        symbol = signal["symbol"]
        held = self.state.setdefault("held", {})
        if symbol in held:
            log.info("spot skip %s: already held", symbol)
            return None
        amount = float(config.SPOT_TRADE_USDT)
        if amount <= 0:
            log.warning("spot skip %s: SPOT_TRADE_USDT <= 0", symbol)
            return None
        try:
            usdt = self.market.usdt_balance()
        except Exception as e:  # noqa: BLE001
            log.warning("spot skip %s: balance read failed: %s", symbol, e)
            return None
        if usdt < amount:
            log.info("spot skip %s: USDT %.2f < trade %.2f",
                     symbol, usdt, amount)
            return None
        try:
            order = self.market.buy_market_quote(symbol, amount)
        except Exception as e:  # noqa: BLE001
            log.error("spot buy failed %s: %s", symbol, e)
            return None
        try:
            qty = float(order.get("executedQty") or 0)
            spent = float(order.get("cummulativeQuoteQty") or 0)
        except (TypeError, ValueError):
            qty, spent = 0.0, 0.0
        if qty <= 0 or spent <= 0:
            log.error("spot buy %s: empty fill in order %s", symbol, order)
            return None
        entry = spent / qty
        rec = {
            "symbol": symbol,
            "qty": qty,
            "entry": entry,
            "spent_usdt": spent,
            "tp1": float(signal["tp1"]),
            "tp2": float(signal["tp2"]),
            "at": int(time.time() * 1000),
            "quality": signal.get("quality"),
        }
        held[symbol] = rec
        self.state.setdefault("history", []).append(rec)
        sid = signal.get("_sid")
        if sid:
            try:
                self.store.mark(sid, "opened")
            except Exception as e:  # noqa: BLE001
                log.warning("spot mark %s failed: %s", sid, e)
        self._save_state()
        log.info("spot BOUGHT %s qty=%s entry~%.6g tp1=%.6g tp2=%.6g",
                 symbol, qty, entry, rec["tp1"], rec["tp2"])
        return rec

    # -------------------------------------------------------------- exits
    def check_take_profit(self) -> list[dict]:
        """Full-close every held position at/above TP1 (bot-side exit).

        Positions whose base asset balance vanished (sold externally)
        are dropped from state without an order."""
        held = self.state.get("held", {})
        if not held:
            return []
        closed = []
        for symbol, rec in list(held.items()):
            try:
                price = self.market.get_ticker_price(symbol)
            except Exception as e:  # noqa: BLE001
                log.warning("spot TP %s: price read failed: %s", symbol, e)
                continue
            if price < float(rec["tp1"]):
                continue
            base = symbol[:-len(config.QUOTE)] if symbol.endswith(config.QUOTE) else symbol
            try:
                bal = self.market.asset_balance(base)
            except Exception:  # noqa: BLE001
                bal = float(rec["qty"])
            qty = min(float(rec["qty"]), bal) if bal > 0 else 0.0
            if qty <= 0:
                log.info("spot TP %s: asset gone externally — dropping record",
                         symbol)
                held.pop(symbol, None)
                continue
            try:
                self.market.sell_market_qty(symbol, qty)
            except Exception as e:  # noqa: BLE001
                log.error("spot TP sell failed %s: %s", symbol, e)
                continue
            rec = dict(rec)
            rec["exit_price"] = price
            rec["exit_at"] = int(time.time() * 1000)
            rec["pnl_usdt"] = qty * (price - float(rec["entry"]))
            held.pop(symbol, None)
            closed.append(rec)
            log.info("spot TP %s: sold qty=%s @%.6g pnl=%+.2f USDT",
                     symbol, qty, price, rec["pnl_usdt"])
        if closed:
            self._save_state()
        return closed

    def process_spot_signals(self) -> list[dict]:
        """One cycle: TP exits first, then buy fresh signals."""
        settings.apply_to_config()
        results = list(self.check_take_profit())
        for sig in self.store.fresh(limit=10):
            rec = self.buy_from_signal(sig)
            if rec:
                results.append(rec)
        return results
