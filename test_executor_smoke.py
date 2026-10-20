"""Executor smoke tests — signal consumed-marking + pending-order guard.

No network: uses a fake market and an isolated temp DATA_DIR.
"""

import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config

_tmp = tempfile.mkdtemp(prefix="bb_exec_")
config.DATA_DIR = _tmp
config.SIGNALS_DB = os.path.join(_tmp, "signals.db")
config.STATE_FILE = os.path.join(_tmp, "state.json")
config.LOG_FILE = os.path.join(_tmp, "bot.log")

from executor import Executor  # noqa: E402
from scanner import SignalStore  # noqa: E402


class FakeMarket:
    def __init__(self):
        self.api_key = "k"
        self.api_secret = "s"
        self._positions = []
        self._open_orders = []
        self.placed = []

    def positions(self):
        return list(self._positions)

    def open_orders(self, symbol):
        return [o for o in self._open_orders if o.get("symbol") == symbol]

    def balance_usdt(self):
        return 1000.0

    def symbol_filters(self, symbol):
        return {"tickSize": 0.01, "stepSize": 0.001, "minQty": 0.001,
                "pricePrecision": 2, "quantityPrecision": 3}

    def set_margin_type(self, *a, **k):
        pass

    def set_leverage(self, *a, **k):
        pass

    def place_order(self, symbol, side, quantity, order_type="MARKET",
                    price=None, reduce_only=False, stop_price=None,
                    working_type="MARK_PRICE", client_id=None):
        self.placed.append({"symbol": symbol, "clientOrderId": client_id or ""})
        return {"orderId": 1, "clientOrderId": client_id}


def _signal(ts=None):
    return {"symbol": "TESTUSDT", "direction": "LONG", "entry": 100.0,
            "stop_loss": 98.0, "tp1": 103.0, "quality": 80,
            "timestamp": int(time.time() * 1000)}


def _executor(store, market):
    ex = Executor.__new__(Executor)
    ex.market = market
    ex.store = store
    ex.state_path = os.path.join(config.DATA_DIR, "executor_state.json")
    ex.state = ex._load_state()
    return ex


def test_signal_marked_opened_and_not_reopened():
    store = SignalStore()
    mkt = FakeMarket()
    store.upsert(_signal())
    rows = store.fresh()
    assert len(rows) == 1 and "_sid" in rows[0]
    ex = _executor(store, mkt)
    recs = ex.process_fresh_signals()
    assert len(recs) == 1, "expected one trade opened"
    assert len(mkt.placed) == 3, "entry + SL + TP expected"
    # signal must now be consumed
    assert store.fresh() == [], "signal should be marked opened"
    # second run must not reopen
    recs2 = ex.process_fresh_signals()
    assert recs2 == [], "must not re-open the same signal"
    assert len(mkt.placed) == 3
    store.close()


def test_pending_entry_order_blocks_reopen():
    store = SignalStore()
    mkt = FakeMarket()
    store.upsert(_signal())
    mkt._open_orders = [{"symbol": "TESTUSDT", "clientOrderId": "bb1729000000TESTUS"}]
    ex = _executor(store, mkt)
    recs = ex.process_fresh_signals()
    assert recs == [], "pending entry order should block re-open"
    assert len(mkt.placed) == 0
    store.close()


def test_executor_close_releases_store():
    import sqlite3
    store = SignalStore()
    mkt = FakeMarket()
    ex = _executor(store, mkt)
    ex.close()
    try:
        store.fresh()
    except sqlite3.ProgrammingError:
        pass  # expected: connection closed
    else:
        raise AssertionError("store connection should be closed")


if __name__ == "__main__":
    test_signal_marked_opened_and_not_reopened()
    test_pending_entry_order_blocks_reopen()
    test_executor_close_releases_store()
    print("executor smoke ok")
