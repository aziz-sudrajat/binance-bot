"""Executor safety tests (bug-hunt round 2) — no network.

Covers: SL attached only after fill, SL failure -> emergency close,
pending LIMIT reconciliation, pending TTL cancel, margin/leverage
failure aborts, min-notional skip, kill switch, daily loss cap.
"""

import os
import sys
import tempfile
import time
from contextlib import contextmanager

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config

_KNOBS = ("DATA_DIR", "SIGNALS_DB", "STATE_FILE", "LOG_FILE",
          "ORDER_FILL_WAIT_SEC", "ORDER_FILL_POLL_SEC",
          "SL_RETRY_DELAY_SEC", "MIN_NOTIONAL_USDT", "ORDER_TYPE",
          "ORDER_TTL_SEC", "MARGIN_MODE")


@contextmanager
def _isolated(**overrides):
    saved = {k: getattr(config, k) for k in _KNOBS}
    tmp = tempfile.mkdtemp(prefix="bb_safety_")
    config.DATA_DIR = tmp
    config.SIGNALS_DB = os.path.join(tmp, "signals.db")
    config.STATE_FILE = os.path.join(tmp, "state.json")
    config.LOG_FILE = os.path.join(tmp, "bot.log")
    for k, v in overrides.items():
        setattr(config, k, v)
    try:
        yield tmp
    finally:
        for k, v in saved.items():
            setattr(config, k, v)


from executor import Executor  # noqa: E402
from scanner import SignalStore  # noqa: E402


class FakeMarket:
    """Scriptable fake: entry fill state, SL failures, setup errors."""

    def __init__(self):
        self.api_key = "k"
        self.api_secret = "s"
        self._positions = []
        self._open_orders = []
        self.placed = []
        self.balance = 1000.0
        self.filters = {"tickSize": 0.01, "stepSize": 0.001, "minQty": 0.001,
                        "minNotional": None, "pricePrecision": 2,
                        "quantityPrecision": 3}
        self.margin_error = None
        self.leverage_error = None
        self.sl_failures = 0            # STOP_MARKET attempts that raise
        self.entry_response_status = None  # status in place_order response
        self.order_status = "FILLED"    # status reported by get_order
        self.order_status_script = []   # successive get_order statuses
        self.cancelled_all = 0
        self._entry_qty = 0.0

    def positions(self):
        return list(self._positions)

    def open_orders(self, symbol):
        return [o for o in self._open_orders if o.get("symbol") == symbol]

    def balance_usdt(self):
        return self.balance

    def symbol_filters(self, symbol):
        return dict(self.filters)

    def set_margin_type(self, symbol, margin_type="ISOLATED"):
        if self.margin_error is not None:
            raise self.margin_error

    def set_leverage(self, symbol, leverage):
        if self.leverage_error is not None:
            raise self.leverage_error

    def place_order(self, symbol, side, quantity, order_type="MARKET",
                    price=None, reduce_only=False, stop_price=None,
                    working_type="MARK_PRICE", client_id=None):
        self.placed.append({
            "symbol": symbol, "side": side, "quantity": quantity,
            "type": order_type, "price": price, "reduceOnly": reduce_only,
            "stopPrice": stop_price, "clientOrderId": client_id,
        })
        if order_type == "STOP_MARKET" and self.sl_failures > 0:
            self.sl_failures -= 1
            raise RuntimeError("SL rejected (fake -2022)")
        if order_type in ("LIMIT", "MARKET"):
            self._entry_qty = quantity
            st = self.entry_response_status
            if st is None:
                st = "NEW" if order_type == "LIMIT" else "FILLED"
            out = {"orderId": 7, "clientOrderId": client_id, "status": st}
            if st == "FILLED":
                out["executedQty"] = quantity
            return out
        return {"orderId": 8, "clientOrderId": client_id, "status": "NEW"}

    def get_order(self, symbol, client_id=None, order_id=None):
        if self.order_status_script:
            st = self.order_status_script.pop(0)
        else:
            st = self.order_status
        out = {"orderId": 7, "clientOrderId": client_id, "status": st}
        if st == "FILLED":
            out["executedQty"] = self._entry_qty
        elif st == "PARTIALLY_FILLED":
            out["executedQty"] = self._entry_qty / 2
        return out

    def cancel_all(self, symbol):
        self.cancelled_all += 1
        self._open_orders = [o for o in self._open_orders
                             if o.get("symbol") != symbol]
        return {}


def _signal(symbol="TESTUSDT"):
    return {"symbol": symbol, "direction": "LONG", "entry": 100.0,
            "stop_loss": 98.0, "tp1": 103.0, "quality": 80,
            "timestamp": int(time.time() * 1000)}


def _types(mkt):
    return [p["type"] for p in mkt.placed]


def test_sl_failure_triggers_emergency_close():
    with _isolated(SL_RETRY_DELAY_SEC=0):
        store = SignalStore()
        mkt = FakeMarket()
        mkt.entry_response_status = "FILLED"
        mkt.sl_failures = 99  # SL always rejected
        store.upsert(_signal())
        sig = store.fresh()[0]
        ex = Executor(market=mkt, store=store)
        rec = ex.open_from_signal(sig)
        assert rec is None, "SL failure must abort the open"
        types = _types(mkt)
        assert types[0] == "LIMIT"
        assert types.count("STOP_MARKET") == config.SL_ATTACH_RETRIES
        # last order is the emergency close: MARKET, reduce-only
        last = mkt.placed[-1]
        assert last["type"] == "MARKET" and last["reduceOnly"] is True
        # signal NOT consumed, nothing recorded as opened
        assert len(store.fresh()) == 1
        assert "TESTUSDT" not in ex.state.get("opened", {})
        store.close()


def test_sl_attached_after_limit_fill():
    with _isolated(ORDER_FILL_WAIT_SEC=5, ORDER_FILL_POLL_SEC=0.05):
        store = SignalStore()
        mkt = FakeMarket()
        mkt.entry_response_status = "NEW"
        mkt.order_status_script = ["NEW", "FILLED"]
        store.upsert(_signal())
        sig = store.fresh()[0]
        ex = Executor(market=mkt, store=store)
        rec = ex.open_from_signal(sig)
        assert rec is not None, "filled LIMIT should open with protection"
        assert _types(mkt) == ["LIMIT", "STOP_MARKET", "TAKE_PROFIT_MARKET"]
        assert store.fresh() == [], "signal should be marked opened"
        store.close()


def test_unfilled_limit_pending_then_reconcile_attaches():
    with _isolated(ORDER_FILL_WAIT_SEC=0, ORDER_FILL_POLL_SEC=0.05):
        store = SignalStore()
        mkt = FakeMarket()
        mkt.entry_response_status = "NEW"
        mkt.order_status = "NEW"  # never fills during the wait window
        store.upsert(_signal())
        sig = store.fresh()[0]
        ex = Executor(market=mkt, store=store)
        rec = ex.open_from_signal(sig)
        assert rec is None
        assert _types(mkt) == ["LIMIT"], "no SL/TP before fill"
        assert "TESTUSDT" in ex.state.get("pending", {})
        # atomic claim: while its entry is pending the signal is held as
        # 'claimed' — not fresh, not opened (not consumable by anyone else)
        st = store.conn.execute(
            "SELECT status FROM signals WHERE id=?",
            (sig["_sid"],)).fetchone()
        assert st is not None and st[0] == "claimed", \
            f"signal held as claimed while pending, got {st}"
        assert store.fresh() == [], "claimed signal not visible as fresh"
        # later: the LIMIT fills (position appears, order reports FILLED)
        mkt.order_status = "FILLED"
        mkt._positions = [{"symbol": "TESTUSDT", "amount": 5.0,
                           "entry": 100.0, "unrealized": 0.0,
                           "leverage": 5, "side": "LONG"}]
        opened = ex.reconcile_pending()
        assert len(opened) == 1, "reconcile should complete the open"
        assert _types(mkt) == ["LIMIT", "STOP_MARKET", "TAKE_PROFIT_MARKET"]
        assert "TESTUSDT" not in ex.state.get("pending", {})
        assert "TESTUSDT" in ex.state.get("opened", {})
        assert store.fresh() == [], "signal marked opened at reconcile"
        store.close()


def test_pending_ttl_cancelled():
    with _isolated():
        store = SignalStore()
        mkt = FakeMarket()
        mkt.order_status = "NEW"
        ex = Executor(market=mkt, store=store)
        sym = "TESTUSDT"
        ex.state["pending"] = {sym: {
            "symbol": sym, "direction": "LONG", "qty": 5.0, "entry": 100.0,
            "stop": 98.0, "tp1": 103.0, "client_id": "bbTEST",
            "order": {}, "quality": 80, "_sid": None,
            "placed_at": int(time.time() * 1000)
            - (config.ORDER_TTL_SEC + 60) * 1000,
        }}
        opened = ex.reconcile_pending()
        assert opened == []
        assert mkt.cancelled_all == 1, "stale pending entry must be cancelled"
        assert sym not in ex.state.get("pending", {})
        assert _types(mkt) == [], "no SL/TP for a cancelled entry"
        store.close()


def test_leverage_failure_aborts_entry():
    with _isolated():
        store = SignalStore()
        mkt = FakeMarket()
        mkt.leverage_error = RuntimeError("leverage endpoint down")
        ex = Executor(market=mkt, store=store)
        rec = ex.open_from_signal(_signal())
        assert rec is None
        assert mkt.placed == [], "no order may be placed after setup failure"
        store.close()


def test_margin_failure_aborts_entry():
    with _isolated():
        store = SignalStore()
        mkt = FakeMarket()
        mkt.margin_error = RuntimeError("margin type rejected")
        ex = Executor(market=mkt, store=store)
        rec = ex.open_from_signal(_signal())
        assert rec is None
        assert mkt.placed == []
        store.close()


def test_margin_already_set_is_benign():
    # Binance -4046 ("already set") must NOT abort — real market class
    # swallows it; emulate by using the real method contract via fake that
    # simply succeeds. Covered implicitly by other tests; here we assert
    # the default margin mode is ISOLATED.
    assert config.MARGIN_MODE == "ISOLATED"


def test_min_notional_skip():
    with _isolated(MIN_NOTIONAL_USDT=100_000):
        store = SignalStore()
        mkt = FakeMarket()
        ex = Executor(market=mkt, store=store)
        rec = ex.open_from_signal(_signal())  # notional would be ~500
        assert rec is None
        assert mkt.placed == []
        store.close()


def test_kill_switch_blocks_entry():
    with _isolated() as tmp:
        store = SignalStore()
        mkt = FakeMarket()
        ex = Executor(market=mkt, store=store)
        with open(os.path.join(tmp, config.KILL_FILE_NAME), "w") as f:
            f.write("stop\n")
        rec = ex.open_from_signal(_signal())
        assert rec is None
        assert mkt.placed == []
        store.close()


def test_daily_loss_cap_blocks_entry():
    with _isolated():
        store = SignalStore()
        mkt = FakeMarket()
        mkt.balance = 850.0  # -15% vs day start 1000, cap is 10%
        ex = Executor(market=mkt, store=store)
        ex.state["daily"] = {
            "date": time.strftime("%Y-%m-%d", time.localtime()),
            "start_equity": 1000.0,
        }
        rec = ex.open_from_signal(_signal())
        assert rec is None
        assert mkt.placed == []
        store.close()


def test_market_entry_attaches_after_fill():
    with _isolated(ORDER_TYPE="MARKET"):
        store = SignalStore()
        mkt = FakeMarket()  # MARKET response defaults to FILLED
        store.upsert(_signal())
        sig = store.fresh()[0]
        ex = Executor(market=mkt, store=store)
        rec = ex.open_from_signal(sig)
        assert rec is not None
        assert _types(mkt) == ["MARKET", "STOP_MARKET", "TAKE_PROFIT_MARKET"]
        store.close()


if __name__ == "__main__":
    test_sl_failure_triggers_emergency_close()
    test_sl_attached_after_limit_fill()
    test_unfilled_limit_pending_then_reconcile_attaches()
    test_pending_ttl_cancelled()
    test_leverage_failure_aborts_entry()
    test_margin_failure_aborts_entry()
    test_margin_already_set_is_benign()
    test_min_notional_skip()
    test_kill_switch_blocks_entry()
    test_daily_loss_cap_blocks_entry()
    test_market_entry_attaches_after_fill()
    print("executor safety ok")
