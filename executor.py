"""Auto-trader — place LONG/SHORT entries + SL/TP on Binance Futures.

Safety model:
- SL/TP are attached ONLY after the entry order has actually filled
  (MARKET: fill verified; LIMIT: polled up to ORDER_FILL_WAIT_SEC, then
  the entry is tracked as "pending" in state and reconciled on later
  passes — fills get their SL/TP then, stale entries are cancelled
  after ORDER_TTL_SEC).
- If the SL cannot be placed after a fill (retries exhausted), the
  position is emergency-closed with a reduce-only MARKET order and the
  signal is NOT marked opened. A naked position is never left behind
  silently.
- Margin/leverage setup failures abort the entry.
- A KILL file in DATA_DIR and the daily loss cap block new entries.
"""

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

_STILL_PENDING = object()  # reconcile sentinel: entry still working


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

    def close(self) -> None:
        """Release the signal-store DB connection."""
        try:
            self.store.close()
        except Exception:  # noqa: BLE001
            pass

    def can_trade(self) -> tuple[bool, str]:
        mode = config.TRADE_MODE
        if mode == "paper":
            return False, "paper mode — signals only"
        if mode == "live" and config.LIVE_CONFIRM != "YES":
            return False, "live mode requires LIVE_CONFIRM=YES"
        if not self.market.api_key or not self.market.api_secret:
            return False, "missing BINANCE_API_KEY / BINANCE_API_SECRET"
        return True, "ok"

    # ------------------------------------------------------------ guards
    def _kill_active(self) -> bool:
        return os.path.exists(os.path.join(config.DATA_DIR,
                                           config.KILL_FILE_NAME))

    def _daily_blocked(self, equity: float) -> bool:
        """Block entries once today's equity drops past the loss cap.

        The first equity seen each day becomes the day's baseline and is
        persisted in executor state."""
        today = time.strftime("%Y-%m-%d", time.localtime())
        daily = self.state.get("daily")
        if not isinstance(daily, dict) or daily.get("date") != today:
            self.state["daily"] = {"date": today,
                                   "start_equity": float(equity)}
            self._save_state()
            return False
        start = float(daily.get("start_equity") or 0)
        if start > 0 and equity < start * (1 - config.DAILY_LOSS_CAP_PCT / 100.0):
            log.warning("daily loss cap: equity %.2f vs day start %.2f "
                        "(cap %.1f%%)", equity, start, config.DAILY_LOSS_CAP_PCT)
            return True
        return False

    def _position_for(self, symbol: str) -> dict | None:
        try:
            for p in self.market.positions():
                if p.get("symbol") == symbol and abs(float(p.get("amount") or 0)) > 0:
                    return p
        except Exception:  # noqa: BLE001
            return None
        return None

    # -------------------------------------------------------------- sizing
    def position_qty(self, symbol: str, entry: float, stop: float,
                     equity: float) -> float:
        """Trade size. With FIXED_MARGIN_USD > 0: margin x leverage /
        entry (the live strict-bot model — margin per entry is fixed,
        so a full book of MAX_OPEN_POSITIONS trades always fits the
        account). Otherwise risk-based: risk_pct of equity /
        |entry-stop|."""
        filt = self.market.symbol_filters(symbol)
        if config.FIXED_MARGIN_USD > 0:
            if entry <= 0:
                return 0.0
            lev = max(1, int(config.MAX_LEVERAGE))
            raw_qty = config.FIXED_MARGIN_USD * lev / entry
            qty = _round_step(raw_qty, filt["stepSize"])
            return qty if qty >= filt["minQty"] else 0.0
        risk_frac = max(0.1, config.RISK_PCT) / 100.0
        risk_usd = equity * risk_frac
        stop_dist = abs(entry - stop)
        if stop_dist <= 0 or entry <= 0:
            return 0.0
        raw_qty = risk_usd / stop_dist
        # leverage does not change notional risk formula above
        qty = _round_step(raw_qty, filt["stepSize"])
        if qty < filt["minQty"]:
            return 0.0
        return qty

    # --------------------------------------------------------- fill handling
    def _await_fill(self, symbol: str, client_id: str, order: dict,
                    want_qty: float, is_limit: bool) -> tuple[str, float]:
        """Resolve an entry order's fill state.

        Returns ("filled", qty) | ("pending", 0) | ("dead", 0). "pending"
        is only returned for LIMIT orders still working after the wait
        window; the caller records them for later reconciliation."""
        status = str(order.get("status") or "").upper()
        eq = float(order.get("executedQty") or 0)
        if status == "FILLED":
            return "filled", eq if eq > 0 else want_qty
        if status in ("CANCELED", "EXPIRED", "REJECTED"):
            return "dead", 0.0
        if not hasattr(self.market, "get_order"):
            # Duck-typed markets (unit-test fakes) cannot be polled: fall
            # back to position presence, else assume the venue filled.
            pos = self._position_for(symbol)
            if pos is not None:
                return "filled", abs(float(pos.get("amount") or want_qty))
            return "filled", want_qty
        deadline = time.time() + max(0.0, float(config.ORDER_FILL_WAIT_SEC))
        last_eq = eq
        while True:
            try:
                o = self.market.get_order(symbol, client_id=client_id)
            except Exception:  # noqa: BLE001
                o = None
            if isinstance(o, dict):
                st = str(o.get("status") or "").upper()
                last_eq = float(o.get("executedQty") or 0)
                if st == "FILLED":
                    return "filled", last_eq if last_eq > 0 else want_qty
                if st in ("CANCELED", "EXPIRED", "REJECTED"):
                    return "dead", 0.0
            if time.time() >= deadline:
                break
            time.sleep(max(0.05, float(config.ORDER_FILL_POLL_SEC)))
        pos = self._position_for(symbol)
        if pos is not None:
            return "filled", abs(float(pos.get("amount") or want_qty))
        if last_eq > 0:
            # partial fill: cancel the remainder, protect what filled
            try:
                self.market.cancel_all(symbol)
            except Exception as e:  # noqa: BLE001
                log.warning("cancel partial %s: %s", symbol, e)
            return "filled", last_eq
        return ("pending", 0.0) if is_limit else ("dead", 0.0)

    def _attach_protection(self, symbol: str, close_side: str, qty: float,
                           stop: float, tp1: float, client_id: str) -> bool:
        """Place SL (retried) + TP1 for a filled position.

        If the SL cannot be placed, the position is emergency-closed
        with a reduce-only MARKET order. Returns True only when the SL
        is in place."""
        sl_ok = False
        attempts = max(1, int(config.SL_ATTACH_RETRIES))
        for attempt in range(1, attempts + 1):
            try:
                self.market.place_order(
                    symbol, close_side, qty, order_type="STOP_MARKET",
                    stop_price=stop, reduce_only=True,
                    client_id=(client_id + "sl")[:36])
                sl_ok = True
                break
            except Exception as e:  # noqa: BLE001
                log.warning("SL attach %s attempt %d/%d failed: %s",
                            symbol, attempt, attempts, e)
                if attempt < attempts:
                    time.sleep(max(0.0, float(config.SL_RETRY_DELAY_SEC)))
        if not sl_ok:
            log.error("SL FAILED %s after %d attempts — EMERGENCY CLOSE",
                      symbol, attempts)
            try:
                self.market.place_order(
                    symbol, close_side, qty, order_type="MARKET",
                    reduce_only=True, client_id=(client_id + "ec")[:36])
                log.error("emergency close %s submitted", symbol)
            except Exception as e:  # noqa: BLE001
                log.critical("EMERGENCY CLOSE FAILED %s: %s — POSITION "
                             "NAKED, ACT MANUALLY", symbol, e)
            return False
        try:
            self.market.place_order(
                symbol, close_side, qty, order_type="TAKE_PROFIT_MARKET",
                stop_price=tp1, reduce_only=True,
                client_id=(client_id + "tp")[:36])
        except Exception as e:  # noqa: BLE001
            log.warning("TP failed %s: %s", symbol, e)
        return True

    def _complete_open(self, prec: dict, qty: float,
                       order: dict | None = None) -> dict | None:
        """Attach SL/TP to a filled entry and record it as opened.

        Returns the record, or None when protection failed (the position
        has been emergency-closed; the signal is NOT marked opened)."""
        symbol = prec["symbol"]
        close_side = "SELL" if prec["direction"] == "LONG" else "BUY"
        if not self._attach_protection(symbol, close_side, qty,
                                       float(prec["stop"]),
                                       float(prec["tp1"]),
                                       prec["client_id"]):
            return None
        rec = {
            "symbol": symbol,
            "direction": prec["direction"],
            "qty": qty,
            "entry": prec["entry"],
            "stop": prec["stop"],
            "tp1": prec["tp1"],
            "order": order if order is not None else prec.get("order"),
            "at": int(time.time() * 1000),
            "quality": prec.get("quality"),
        }
        self.state.setdefault("opened", {})[symbol] = rec
        self.state.setdefault("history", []).append(rec)
        # mark signal consumed so the next trade/loop run doesn't re-open it
        sid = prec.get("_sid")
        if sid:
            try:
                self.store.mark(sid, "opened")
            except Exception as e:  # noqa: BLE001
                log.warning("mark opened %s: %s", sid, e)
        return rec

    # --------------------------------------------------------- reconciliation
    def reconcile_pending(self) -> list[dict]:
        """Resolve pending LIMIT entries recorded in state.

        Filled since placement -> attach SL/TP now. Gone without a fill
        -> free the symbol. Still working past ORDER_TTL_SEC -> cancel.
        Runs on the normal trade/loop path before fresh signals."""
        ok, _why = self.can_trade()
        if not ok:
            return []
        pending = self.state.get("pending") or {}
        if not pending:
            return []
        opened: list[dict] = []
        changed = False
        for symbol, prec in list(pending.items()):
            try:
                rec = self._reconcile_one(symbol, prec)
            except Exception as e:  # noqa: BLE001
                log.warning("reconcile %s failed: %s", symbol, e)
                continue
            if rec is _STILL_PENDING:
                continue
            pending.pop(symbol, None)
            changed = True
            if isinstance(rec, dict):
                opened.append(rec)
        if changed:
            self._save_state()
        return opened

    def _reconcile_one(self, symbol: str, prec: dict):
        client_id = prec.get("client_id")
        order = None
        if client_id and hasattr(self.market, "get_order"):
            try:
                order = self.market.get_order(symbol, client_id=client_id)
            except Exception:  # noqa: BLE001
                order = None
        pos = self._position_for(symbol)
        status = str((order or {}).get("status") or "").upper()
        eq = float((order or {}).get("executedQty") or 0)
        if pos is not None or status == "FILLED":
            qty = (abs(float(pos.get("amount"))) if pos is not None
                   else (eq if eq > 0 else float(prec["qty"])))
            log.info("reconcile %s: entry filled qty=%s — attaching SL/TP",
                     symbol, qty)
            return self._complete_open(prec, qty)
        if status in ("CANCELED", "EXPIRED", "REJECTED"):
            log.info("reconcile %s: entry order %s — freeing symbol",
                     symbol, status)
            try:
                if prec.get("_sid"):
                    self.store.release_claim(prec["_sid"])
            except Exception:  # noqa: BLE001
                pass
            return None
        age_ms = int(time.time() * 1000) - int(prec.get("placed_at") or 0)
        if order is None and pos is None:
            # Order unknown and no position: only free the symbol after a
            # grace period (the venue may just be lagging).
            if age_ms > 60_000:
                log.warning("reconcile %s: order vanished without fill — "
                            "freeing symbol", symbol)
                try:
                    if prec.get("_sid"):
                        self.store.release_claim(prec["_sid"])
                except Exception:  # noqa: BLE001
                    pass
                return None
            return _STILL_PENDING
        if eq > 0:
            # partial fill: cancel the remainder, protect what filled
            try:
                self.market.cancel_all(symbol)
            except Exception as e:  # noqa: BLE001
                log.warning("reconcile %s cancel partial: %s", symbol, e)
            log.info("reconcile %s: partial fill qty=%s — attaching SL/TP",
                     symbol, eq)
            return self._complete_open(prec, eq)
        if age_ms > config.ORDER_TTL_SEC * 1000:
            try:
                self.market.cancel_all(symbol)
                log.info("reconcile %s: pending entry past TTL %ds — "
                         "cancelled", symbol, config.ORDER_TTL_SEC)
            except Exception as e:  # noqa: BLE001
                log.warning("reconcile %s TTL cancel failed: %s", symbol, e)
            try:
                if prec.get("_sid"):
                    self.store.release_claim(prec["_sid"])
            except Exception:  # noqa: BLE001
                pass
            return None
        return _STILL_PENDING

    # -------------------------------------------------------------- entries
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

        # --- atomic claim: only one process may work this signal ---
        sid = signal.get("_sid")
        claimed = False
        if sid:
            try:
                claimed = bool(self.store.claim(sid))
            except Exception as e:  # noqa: BLE001
                # claim() may have partially applied: release defensively
                # so no signal leaks in 'claimed' state without a release.
                log.error("abort %s: claim(%s) raised %s", symbol, sid, e)
                try:
                    self.store.release_claim(sid)
                except Exception:  # noqa: BLE001
                    pass
                return None
            if not claimed:
                log.info("skip %s: signal %s already claimed by another "
                         "process", symbol, sid)
                return None

        def _release_claim():
            if claimed:
                try:
                    self.store.release_claim(sid)
                except Exception as e:  # noqa: BLE001
                    log.warning("release claim %s: %s", sid, e)

        if self._kill_active():
            _release_claim()
            log.warning("skip %s: kill switch active (%s)", symbol,
                        os.path.join(config.DATA_DIR, config.KILL_FILE_NAME))
            return None

        # one position per symbol
        open_pos = self.market.positions()
        if any(p["symbol"] == symbol for p in open_pos):
            _release_claim()
            log.info("skip %s: already in position", symbol)
            return None
        # a LIMIT entry of ours is still waiting for its fill
        if symbol in (self.state.get("pending") or {}):
            _release_claim()
            log.info("skip %s: pending entry awaiting fill", symbol)
            return None
        # guard: pending entry order from an earlier attempt (LIMIT may sit unfilled)
        try:
            pending_orders = [o for o in self.market.open_orders(symbol)
                              if str(o.get("clientOrderId", "")).startswith("bb")]
        except Exception:  # noqa: BLE001
            pending_orders = []
        if pending_orders:
            _release_claim()
            log.info("skip %s: %d pending entry order(s)", symbol,
                     len(pending_orders))
            return None
        if len(open_pos) >= config.MAX_OPEN_POSITIONS:
            _release_claim()
            log.info("skip %s: max open positions %d", symbol,
                     config.MAX_OPEN_POSITIONS)
            return None

        equity = self.market.equity_usdt()
        if equity <= 0:
            _release_claim()
            log.warning("no USDT balance")
            return None
        if self._daily_blocked(equity):
            _release_claim()
            log.warning("skip %s: daily loss cap reached", symbol)
            return None

        entry = float(signal["entry"])
        stop = float(signal["stop_loss"])
        tp1 = float(signal["tp1"])
        qty = self.position_qty(symbol, entry, stop, equity)
        if qty <= 0:
            _release_claim()
            log.info("skip %s: qty=0 (risk too small)", symbol)
            return None

        filt = self.market.symbol_filters(symbol)
        entry = _round_tick(entry, filt["tickSize"])
        stop = _round_tick(stop, filt["tickSize"])
        tp1 = _round_tick(tp1, filt["tickSize"])

        min_notional = max(config.MIN_NOTIONAL_USDT,
                           float(filt.get("minNotional") or 0))
        if qty * entry < min_notional:
            _release_claim()
            log.info("skip %s: notional %.2f < min %.2f",
                     symbol, qty * entry, min_notional)
            return None

        # margin/leverage setup is part of the trade's risk definition:
        # if it cannot be applied, the entry must not happen at all.
        try:
            self.market.set_margin_type(symbol, config.MARGIN_MODE)
            self.market.set_leverage(symbol, config.MAX_LEVERAGE)
        except Exception as e:  # noqa: BLE001
            _release_claim()
            log.error("abort %s: margin/leverage setup failed: %s", symbol, e)
            return None

        side = "BUY" if direction == "LONG" else "SELL"
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
            _release_claim()
            log.error("entry failed %s: %s", symbol, e)
            return None

        prec = {
            "symbol": symbol,
            "direction": direction,
            "qty": qty,
            "entry": entry,
            "stop": stop,
            "tp1": tp1,
            "client_id": client_id,
            "order": order,
            "quality": signal.get("quality"),
            "_sid": signal.get("_sid"),
            "placed_at": int(time.time() * 1000),
        }

        fill_state, fill_qty = self._await_fill(
            symbol, client_id, order, qty,
            is_limit=(config.ORDER_TYPE == "LIMIT"))
        if fill_state == "dead":
            _release_claim()
            log.error("entry %s did not fill (order %s)", symbol,
                      str(order.get("status") or "dead"))
            return None
        if fill_state == "pending":
            # LIMIT still working: track it; reconciliation attaches
            # SL/TP when it fills, or cancels it after ORDER_TTL_SEC.
            self.state.setdefault("pending", {})[symbol] = prec
            self._save_state()
            log.info("pending %s: LIMIT entry unfilled — tracked for "
                     "reconciliation", symbol)
            return None

        rec = self._complete_open(prec, fill_qty, order=order)
        if rec is None:
            _release_claim()
            log.error("open %s aborted: SL failed — position "
                      "emergency-closed", symbol)
            return None
        self._save_state()
        return rec

    def check_profit_take(self) -> list[dict]:
        """Bot-side profit take, checked every cycle (live strict-bot
        semantics): any bot-managed position whose unrealized profit
        reaches PROFIT_TAKE_PCT% of its margin is full-closed at
        MARKET. The exchange TP1 order stays as backup. Positions
        that vanished from the exchange are left to reconcile_pending.
        Margin is computed at each position's own exchange leverage.
        Returns the closed records."""
        pct = float(config.PROFIT_TAKE_PCT)
        if pct <= 0:
            return []
        opened = self.state.get("opened", {})
        if not opened:
            return []
        try:
            live = {p["symbol"]: p for p in self.market.positions()}
        except Exception as e:  # noqa: BLE001
            log.warning("profit-take: positions read failed: %s", e)
            return []
        closed = []
        for symbol, rec in list(opened.items()):
            p = live.get(symbol)
            if not p:
                continue
            try:
                qty = abs(float(p["amount"]))
                entry = float(p["entry"])
                upnl = float(p["unrealized"])
                # per-position leverage from the exchange (positions
                # opened under an older leverage stay correct when
                # MAX_LEVERAGE changes mid-flight)
                lev = max(1, int(p.get("leverage") or
                                 config.MAX_LEVERAGE))
            except (TypeError, ValueError):
                continue
            margin = qty * entry / lev if entry > 0 else 0
            if margin <= 0 or upnl < margin * pct / 100.0:
                continue
            close_side = "SELL" if rec.get("direction") == "LONG" else "BUY"
            try:
                self.market.place_order(
                    symbol, close_side, qty, order_type="MARKET",
                    reduce_only=True,
                    client_id=(f"pt{symbol}"[:20]))
                log.info("profit-take %s: +%.2f on margin %.2f "
                         "(%.1f%%) — closed", symbol, upnl, margin, pct)
            except Exception as e:  # noqa: BLE001
                log.error("profit-take %s failed: %s", symbol, e)
                continue
            opened.pop(symbol, None)
            closed.append(rec)
        if closed:
            self._save_state()
        return closed

    def process_fresh_signals(self) -> list[dict]:
        ok, why = self.can_trade()
        if not ok:
            log.info("executor idle: %s", why)
            return []
        results = list(self.reconcile_pending())
        results.extend(self.check_profit_take())
        for sig in self.store.fresh(limit=10):
            rec = self.open_from_signal(sig)
            if rec:
                results.append(rec)
        return results
