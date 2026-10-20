"""Audit tests by azz (round 2) — config knobs, .env loading, signal-store
supersede/WAL, and request signing. No network.

Covers the azz audit lane: config/knob correctness + store race hygiene.
Run: .venv/bin/python test_audit_azz.py  (or via pytest)
"""

import hashlib
import hmac
import os
import subprocess
import sys
import tempfile
from urllib.parse import urlencode

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config

_BOT_DIR = os.path.dirname(os.path.abspath(__file__))


# --------------------------------------------------------------- .env loader
def test_dotenv_loader_parses_and_never_overrides():
    fd, path = tempfile.mkstemp(suffix=".env")
    try:
        with os.fdopen(fd, "w") as f:
            f.write("# comment\n\nBB_AUDIT_A=hello\n")
            f.write('BB_AUDIT_B="quoted value"\nBB_AUDIT_C=kept\n')
        os.environ.pop("BB_AUDIT_A", None)
        os.environ.pop("BB_AUDIT_B", None)
        os.environ["BB_AUDIT_C"] = "original"
        pairs = config._load_dotenv(path)
        assert pairs["BB_AUDIT_A"] == "hello"
        assert pairs["BB_AUDIT_B"] == "quoted value"
        assert os.environ["BB_AUDIT_A"] == "hello"
        # a real exported variable must win over the file
        assert os.environ["BB_AUDIT_C"] == "original"
    finally:
        os.unlink(path)
        for k in ("BB_AUDIT_A", "BB_AUDIT_B"):
            os.environ.pop(k, None)


def test_dotenv_missing_file_is_not_an_error():
    assert config._load_dotenv("/nonexistent/dir/.env") == {}


# ------------------------------------------------------------- mode handling
def test_normalize_mode():
    assert config._normalize_mode("live") == "live"
    assert config._normalize_mode(" Testnet ") == "testnet"
    assert config._normalize_mode("PAPER") == "paper"
    # typos must fail closed to paper, never silently trade
    assert config._normalize_mode("liv") == "paper"
    assert config._normalize_mode("prod") == "paper"
    assert config._normalize_mode("") == "paper"


def _run_config_subprocess(extra_env):
    env = dict(os.environ)
    env.update(extra_env)
    out = subprocess.run(
        [sys.executable, "-c",
         "import config; print(config.TRADE_MODE, config.MARGIN_TYPE,"
         " config.ORDER_TYPE)"],
        capture_output=True, text=True, cwd=_BOT_DIR, env=env, timeout=60)
    assert out.returncode == 0, out.stderr
    return out.stdout.strip()


def test_knob_env_override():
    got = _run_config_subprocess(
        {"TRADE_MODE": "live", "MARGIN_TYPE": "isolated",
         "ORDER_TYPE": "market"})
    assert got == "live ISOLATED MARKET", got


def test_invalid_knobs_fail_closed():
    got = _run_config_subprocess(
        {"TRADE_MODE": "liv", "MARGIN_TYPE": "cross", "ORDER_TYPE": "limt"})
    # mode typo -> paper; margin typo -> ISOLATED (the safe default since
    # shiki's safety batch); order typo must NOT silently become MARKET
    assert got == "paper ISOLATED LIMIT", got


def test_valid_margin_crossed_passes_through():
    got = _run_config_subprocess(
        {"TRADE_MODE": "paper", "MARGIN_TYPE": "CROSSED",
         "ORDER_TYPE": "LIMIT"})
    assert got == "paper CROSSED LIMIT", got


# -------------------------------------------------------------- signal store
def _fresh_store():
    tmp = tempfile.mkdtemp(prefix="bb_audit_")
    config.DATA_DIR = tmp
    config.SIGNALS_DB = os.path.join(tmp, "signals.db")
    from scanner import SignalStore
    return SignalStore()


def _sig(ts, direction="LONG"):
    return {"symbol": "TESTUSDT", "direction": direction, "quality": 80,
            "entry": 100.0, "stop_loss": 98.0, "tp1": 103.0,
            "timestamp": ts}


def test_store_supersedes_stale_duplicate():
    store = _fresh_store()
    store.upsert(_sig(1000))
    store.upsert(_sig(2000))  # newer plan, same symbol+direction
    rows = store.fresh()
    assert len(rows) == 1, f"stale duplicate still fresh: {len(rows)}"
    assert rows[0]["timestamp"] == 2000
    # opposite direction is a different plan — untouched
    store.upsert(_sig(3000, direction="SHORT"))
    assert len(store.fresh()) == 2
    store.close()


def test_store_uses_wal():
    store = _fresh_store()
    mode = store.conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert str(mode).lower() == "wal", mode
    store.close()


def test_store_claim_is_atomic():
    store = _fresh_store()
    sid = store.upsert(_sig(1000))
    assert store.claim(sid) is True
    assert store.claim(sid) is False, "second claim must lose the race"
    assert store.fresh() == [], "claimed signal must not be listed fresh"
    store.release_claim(sid)
    assert len(store.fresh()) == 1, "released claim returns to fresh"
    assert store.claim("nonexistent-id") is False
    store.close()


def test_store_claim_rejects_expired():
    store = _fresh_store()
    sid = store.upsert(_sig(1000), ttl_sec=-5)
    assert store.claim(sid) is False, "expired signal must not be claimable"
    store.close()


def test_store_expire_old_covers_claimed():
    store = _fresh_store()
    sid = store.upsert(_sig(1000), ttl_sec=60)
    assert store.claim(sid) is True
    store.conn.execute("UPDATE signals SET expires_at=1 WHERE id=?", (sid,))
    store.conn.commit()
    assert store.expire_old() == 1, "orphaned claims must expire too"
    row = store.conn.execute(
        "SELECT status FROM signals WHERE id=?", (sid,)).fetchone()
    assert row[0] == "expired"
    store.close()


# ------------------------------------------------------------------ signing
def test_sign_covers_exactly_sent_params():
    from market import BinanceFutures
    m = BinanceFutures(api_key="testkey", api_secret="testsecret",
                       testnet=True)
    signed = m._sign({"symbol": "BTCUSDT", "side": "BUY", "quantity": 0.5})
    sig = signed.pop("signature")
    assert "timestamp" in signed and "recvWindow" in signed
    expected = hmac.new(b"testsecret", urlencode(signed).encode(),
                        hashlib.sha256).hexdigest()
    assert sig == expected, "signature must cover exactly the sent params"


def test_get_resigns_each_attempt():
    """A retried signed GET must carry a fresh timestamp/signature —
    reusing the first attempt's signature goes stale vs recvWindow."""
    from unittest import mock
    from market import BinanceFutures

    class Resp:
        def __init__(self, code, data=None):
            self.status_code = code
            self._data = data or {}

        def raise_for_status(self):
            pass

        def json(self):
            return self._data

    class FakeSession:
        def __init__(self):
            self.calls = []
            self.headers = {}

        def get(self, url, params=None, timeout=None):
            self.calls.append(dict(params or {}))
            if len(self.calls) == 1:
                return Resp(429)
            return Resp(200, {"ok": True})

    m = BinanceFutures(api_key="k", api_secret="s", testnet=True)
    m.session = FakeSession()
    t0 = 1_700_000_000.0
    with mock.patch("time.sleep", lambda *_a: None), \
            mock.patch("time.time", side_effect=[t0, t0 + 60.0]):
        out = m._get("/fapi/v2/balance", signed=True)
    assert out == {"ok": True}
    assert len(m.session.calls) == 2
    first, second = m.session.calls
    assert first["timestamp"] == int(t0 * 1000)
    assert second["timestamp"] == int((t0 + 60.0) * 1000), \
        "retry must re-sign with a fresh timestamp"
    for call in (first, second):
        sig = call.pop("signature")
        expected = hmac.new(b"s", urlencode(call).encode(),
                            hashlib.sha256).hexdigest()
        assert sig == expected


if __name__ == "__main__":
    test_dotenv_loader_parses_and_never_overrides()
    test_dotenv_missing_file_is_not_an_error()
    test_normalize_mode()
    test_knob_env_override()
    test_invalid_knobs_fail_closed()
    test_valid_margin_crossed_passes_through()
    test_store_supersedes_stale_duplicate()
    test_store_uses_wal()
    test_store_claim_is_atomic()
    test_store_claim_rejects_expired()
    test_store_expire_old_covers_claimed()
    test_sign_covers_exactly_sent_params()
    test_get_resigns_each_attempt()
    print("audit azz ok")
