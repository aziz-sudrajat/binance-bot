"""Runtime settings tests — validation, overlay, apply, ISOLATED lock.

No network: temp DATA_DIR, fake market for the executor guard test.
"""

import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
import settings

_tmp = tempfile.mkdtemp(prefix="bb_settings_")
config.DATA_DIR = _tmp
config.SIGNALS_DB = os.path.join(_tmp, "signals.db")
config.STATE_FILE = os.path.join(_tmp, "state.json")
config.LOG_FILE = os.path.join(_tmp, "bot.log")

from executor import Executor  # noqa: E402


def _fresh_tmp():
    tmp = tempfile.mkdtemp(prefix="bb_settings_run_")
    config.DATA_DIR = tmp
    config.SIGNALS_DB = os.path.join(tmp, "signals.db")
    config.STATE_FILE = os.path.join(tmp, "state.json")
    config.LOG_FILE = os.path.join(tmp, "bot.log")
    settings._LAST_APPLIED = None
    return tmp


def test_validation_bounds():
    _fresh_tmp()
    assert settings._coerce("leverage", "20") == 20
    for bad in ("0", "126", "abc"):
        try:
            settings._coerce("leverage", bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"leverage {bad!r} should fail")
    try:
        settings._coerce("tp_roi_pct", "-1")
    except ValueError:
        pass
    else:
        raise AssertionError("negative tp_roi_pct should fail")
    try:
        settings._coerce("unknown_key", "1")
    except ValueError:
        pass
    else:
        raise AssertionError("unknown key should fail")


def test_margin_mode_locked_to_isolated():
    _fresh_tmp()
    assert settings._coerce("margin_mode", "isolated") == "ISOLATED"
    for bad in ("CROSS", "cross", "both"):
        try:
            settings._coerce("margin_mode", bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"margin_mode {bad!r} should fail")
    try:
        settings.set_value("margin_mode", "CROSS")
    except ValueError:
        pass
    else:
        raise AssertionError("storing CROSS should fail")
    assert not os.path.exists(settings.settings_path())


def test_bool_parsing():
    _fresh_tmp()
    assert settings._coerce("btc_align", "off") is False
    assert settings._coerce("btc_align", "ON") is True
    assert settings._coerce("kill_switch", True) is True


def test_effective_overlay_and_apply():
    tmp = _fresh_tmp()
    config.MAX_LEVERAGE = 10
    config.PROFIT_TAKE_PCT = 20.0
    settings.set_value("leverage", "25")
    eff = settings.effective()
    assert eff["leverage"] == 25
    assert eff["tp_roi_pct"] == 20.0  # untouched key keeps env default
    assert settings.apply_to_config() is True
    assert config.MAX_LEVERAGE == 25
    assert settings.apply_to_config() is False  # no change second time
    assert os.path.exists(settings.settings_path())


def test_spot_settings_keys():
    _fresh_tmp()
    assert settings.set_value("spot_trade_usd", "250")["spot_trade_usd"] == 250.0
    assert settings.set_value("spot_tp1_atr", "3")["spot_tp1_atr"] == 3.0
    try:
        settings.set_value("spot_trade_usd", "0")
    except ValueError:
        pass
    else:
        raise AssertionError("spot_trade_usd 0 should fail")


def test_kill_switch_drives_kill_file():
    tmp = _fresh_tmp()
    kill_path = os.path.join(tmp, config.KILL_FILE_NAME)
    settings.set_value("kill_switch", "on")
    settings.apply_to_config()
    assert os.path.exists(kill_path)
    settings.set_value("kill_switch", "off")
    settings.apply_to_config()
    assert not os.path.exists(kill_path)


class _StubStore:
    def fresh(self, limit=10):
        return []

    def mark(self, *a, **k):
        pass

    def claim(self, sid):
        return True

    def release_claim(self, sid):
        pass


class _FakeMarket:
    def __init__(self):
        self.api_key = "k"
        self.api_secret = "s"
        self.margin_calls = []
        self.placed = []

    def positions(self):
        return []

    def open_orders(self, symbol):
        return []

    def balance_usdt(self):
        return 10000.0

    def equity_usdt(self):
        return 10000.0

    def symbol_filters(self, symbol):
        return {"tickSize": 0.01, "stepSize": 0.001, "minQty": 0.001,
                "pricePrecision": 2, "quantityPrecision": 3,
                "minNotional": 5.0}

    def set_margin_type(self, symbol, mode):
        self.margin_calls.append(mode)

    def set_leverage(self, symbol, lev):
        pass

    def place_order(self, symbol, side, quantity, order_type="MARKET",
                    price=None, reduce_only=False, stop_price=None,
                    working_type="MARK_PRICE", client_id=None):
        self.placed.append({"symbol": symbol, "side": side})
        return {"orderId": 1, "clientOrderId": client_id}


def _signal():
    return {"symbol": "TESTUSDT", "direction": "LONG", "entry": 100.0,
            "stop_loss": 99.0, "tp1": 103.0, "quality": 90,
            "timestamp": int(time.time() * 1000)}


def test_executor_refuses_cross_margin():
    _fresh_tmp()
    config.TRADE_MODE = "testnet"
    config.FIXED_MARGIN_USD = 100.0
    config.MAX_LEVERAGE = 10
    config.MARGIN_MODE = "CROSS"
    try:
        m = _FakeMarket()
        ex = Executor(m, _StubStore())
        assert ex.open_from_signal(_signal()) is None
        assert m.margin_calls == []
        assert m.placed == []
    finally:
        config.MARGIN_MODE = "ISOLATED"
        config.FIXED_MARGIN_USD = 0.0


def test_executor_proceeds_on_isolated():
    _fresh_tmp()
    config.TRADE_MODE = "testnet"
    config.FIXED_MARGIN_USD = 100.0
    config.MAX_LEVERAGE = 10
    config.MARGIN_MODE = "ISOLATED"
    try:
        m = _FakeMarket()
        ex = Executor(m, _StubStore())
        ex.open_from_signal(_signal())
        assert m.margin_calls == ["ISOLATED"]
    finally:
        config.FIXED_MARGIN_USD = 0.0
