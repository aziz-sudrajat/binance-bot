"""Spot executor + spot client tests. No network: fake spot market."""

import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config

_tmp = tempfile.mkdtemp(prefix="bb_spot_")
config.DATA_DIR = _tmp
config.SIGNALS_DB = os.path.join(_tmp, "signals.db")
config.SPOT_SIGNALS_DB = os.path.join(_tmp, "spot_signals.db")
config.SPOT_STATE_FILE = os.path.join(_tmp, "spot_state.json")
config.STATE_FILE = os.path.join(_tmp, "state.json")
config.LOG_FILE = os.path.join(_tmp, "bot.log")

from spot_executor import SpotExecutor  # noqa: E402
from spot_market import SpotClient  # noqa: E402


class StubStore:
    def fresh(self, limit=10):
        return []

    def mark(self, *a, **k):
        pass


class FakeSpotMarket:
    def __init__(self):
        self.usdt = 1000.0
        self.balances = {}
        self.prices = {}
        self.sells = []
        self.buys = []

    def usdt_balance(self):
        return self.usdt

    def asset_balance(self, asset):
        return self.balances.get(asset, 0.0)

    def get_ticker_price(self, symbol):
        return self.prices[symbol]

    def buy_market_quote(self, symbol, usdt):
        self.buys.append((symbol, usdt))
        # fill at 2.0 per unit
        return {"executedQty": usdt / 2.0, "cummulativeQuoteQty": usdt}

    def sell_market_qty(self, symbol, qty):
        self.sells.append((symbol, qty))
        return {"executedQty": qty, "cummulativeQuoteQty": qty * 2.5}


def _executor(market):
    tmp = tempfile.mkdtemp(prefix="bb_spot_run_")
    config.DATA_DIR = tmp
    config.SPOT_STATE_FILE = os.path.join(tmp, "spot_state.json")
    return SpotExecutor(market, StubStore())


def _signal():
    return {"symbol": "TESTUSDT", "direction": "LONG", "entry": 2.0,
            "tp1": 2.2, "tp2": 2.4, "quality": 85,
            "timestamp": int(time.time() * 1000)}


def test_spot_buy_records_entry_and_tp():
    config.SPOT_TRADE_USDT = 100.0
    m = FakeSpotMarket()
    ex = _executor(m)
    rec = ex.buy_from_signal(_signal())
    assert rec is not None
    assert rec["qty"] == 50.0 and rec["entry"] == 2.0
    assert ex.state["held"]["TESTUSDT"]["tp1"] == 2.2
    # already held -> skip
    assert ex.buy_from_signal(_signal()) is None
    # below TP1 -> no sell
    m.prices["TESTUSDT"] = 2.1
    assert ex.check_take_profit() == []
    # at TP1 -> full sell, pnl recorded
    m.prices["TESTUSDT"] = 2.5
    m.balances["TEST"] = 50.0
    closed = ex.check_take_profit()
    assert len(closed) == 1
    assert closed[0]["pnl_usdt"] == 50.0 * (2.5 - 2.0)
    assert m.sells == [("TESTUSDT", 50.0)]
    assert "TESTUSDT" not in ex.state["held"]


def test_spot_buy_skips_when_broke_and_drops_gone_asset():
    config.SPOT_TRADE_USDT = 100.0
    m = FakeSpotMarket()
    m.usdt = 50.0
    ex = _executor(m)
    assert ex.buy_from_signal(_signal()) is None
    # held record but asset sold externally -> dropped, no order
    ex.state["held"]["TESTUSDT"] = {"qty": 50.0, "entry": 2.0, "tp1": 2.2,
                                    "tp2": 2.4}
    m.prices["TESTUSDT"] = 3.0
    m.balances["TEST"] = 0.0
    ex.check_take_profit()
    assert "TESTUSDT" not in ex.state["held"]
    assert m.sells == []


class _CannedSpotClient(SpotClient):
    def __init__(self, tickers, klines):
        self._tickers = tickers
        self._klines = klines
        self.api_key = ""
        self.api_secret = ""

    def _get(self, path, params=None, signed=False):
        if "ticker/24hr" in path:
            return self._tickers
        if "klines" in path:
            return self._klines
        raise AssertionError(f"unexpected GET {path}")


def test_spot_top_symbols_filters_stables_and_leveraged():
    tickers = [
        {"symbol": "BTCUSDT", "quoteVolume": "100000000"},
        {"symbol": "USDCUSDT", "quoteVolume": "90000000"},
        {"symbol": "BTCUPUSDT", "quoteVolume": "80000000"},
        {"symbol": "ETHUSDT", "quoteVolume": "50000000"},
        {"symbol": "LOWUSDT", "quoteVolume": "10"},
    ]
    now = int(time.time() * 1000)
    klines = [
        [now - 3600000, "100", "101", "99", "100.5", "10", now - 1, "1000"],
        [now, "100.5", "102", "100", "101", "10", now + 3600000, "1000"],
    ]
    c = _CannedSpotClient(tickers, klines)
    assert c.top_symbols_by_volume(10) == ["BTCUSDT", "ETHUSDT"]
    ks = c.get_klines("BTCUSDT", "1h")
    assert len(ks) == 1 and ks[0]["close"] == 100.5  # unclosed dropped
