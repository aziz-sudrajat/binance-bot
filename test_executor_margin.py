"""Executor margin tests — fixed-margin sizing + bot-side profit take.

No network: fake market, isolated temp DATA_DIR per test.
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config

_tmp = tempfile.mkdtemp(prefix="bb_margin_")
config.DATA_DIR = _tmp
config.SIGNALS_DB = os.path.join(_tmp, "signals.db")
config.STATE_FILE = os.path.join(_tmp, "state.json")
config.LOG_FILE = os.path.join(_tmp, "bot.log")

from executor import Executor  # noqa: E402


class StubStore:
    def fresh(self, limit=10):
        return []

    def mark(self, *a, **k):
        pass


class FakeMarket:
    def __init__(self):
        self._positions = []
        self.placed = []

    def positions(self):
        return list(self._positions)

    def open_orders(self, symbol):
        return []

    def balance_usdt(self):
        return 10000.0

    def equity_usdt(self):
        return 10000.0

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
        self.placed.append({"symbol": symbol, "side": side,
                            "quantity": quantity, "order_type": order_type,
                            "reduce_only": reduce_only})
        return {"orderId": 1, "clientOrderId": client_id}


def _executor(market):
    tmp = tempfile.mkdtemp(prefix="bb_margin_run_")
    config.DATA_DIR = tmp
    config.SIGNALS_DB = os.path.join(tmp, "signals.db")
    config.STATE_FILE = os.path.join(tmp, "state.json")
    config.LOG_FILE = os.path.join(tmp, "bot.log")
    return Executor(market, StubStore())


def test_fixed_margin_qty():
    config.FIXED_MARGIN_USD = 100.0
    config.MAX_LEVERAGE = 5
    try:
        ex = _executor(FakeMarket())
        # 100 USD margin x 5 leverage / 2.0 entry = 250
        assert ex.position_qty("XRPUSDT", 2.0, 1.9, 10000.0) == 250.0
    finally:
        config.FIXED_MARGIN_USD = 0.0


def test_risk_based_qty_unchanged_when_fixed_margin_off():
    config.FIXED_MARGIN_USD = 0.0
    config.RISK_PCT = 1.0
    try:
        ex = _executor(FakeMarket())
        # risk 1% of 10000 = 100 / stop distance 1.0 = 100
        assert ex.position_qty("X", 100.0, 99.0, 10000.0) == 100.0
    finally:
        config.FIXED_MARGIN_USD = 0.0


def test_profit_take_closes_at_threshold():
    config.PROFIT_TAKE_PCT = 20.0
    config.MAX_LEVERAGE = 5
    try:
        m = FakeMarket()
        # margin = 250 x 2.0 / 5 = 100; 20% = 20 -> exactly at threshold
        m._positions = [{"symbol": "XRPUSDT", "amount": 250.0,
                         "entry": 2.0, "unrealized": 20.0}]
        ex = _executor(m)
        ex.state["opened"] = {"XRPUSDT": {"direction": "LONG"}}
        closed = ex.check_profit_take()
        assert len(closed) == 1 and closed[0] is not None
        assert m.placed[0]["side"] == "SELL"
        assert m.placed[0]["order_type"] == "MARKET"
        assert m.placed[0]["reduce_only"] is True
        assert "XRPUSDT" not in ex.state["opened"]
    finally:
        config.PROFIT_TAKE_PCT = 0.0


def test_profit_take_skips_below_threshold_and_gone_positions():
    config.PROFIT_TAKE_PCT = 20.0
    config.MAX_LEVERAGE = 5
    try:
        m = FakeMarket()
        m._positions = [{"symbol": "XRPUSDT", "amount": 250.0,
                         "entry": 2.0, "unrealized": 19.99}]
        ex = _executor(m)
        ex.state["opened"] = {"XRPUSDT": {"direction": "LONG"},
                              "GONEUSDT": {"direction": "SHORT"}}
        closed = ex.check_profit_take()
        assert closed == [] and m.placed == []
        assert "XRPUSDT" in ex.state["opened"]
        assert "GONEUSDT" in ex.state["opened"]
    finally:
        config.PROFIT_TAKE_PCT = 0.0


def test_profit_take_disabled_by_default():
    config.PROFIT_TAKE_PCT = 0.0
    m = FakeMarket()
    m._positions = [{"symbol": "XRPUSDT", "amount": 250.0,
                     "entry": 2.0, "unrealized": 999.0}]
    ex = _executor(m)
    ex.state["opened"] = {"XRPUSDT": {"direction": "LONG"}}
    assert ex.check_profit_take() == [] and m.placed == []
