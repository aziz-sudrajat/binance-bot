"""binance-bot configuration — LONG + SHORT futures signal + auto-trade."""

from __future__ import annotations

import os
import sys


def _load_dotenv(path: str | None = None) -> dict:
    """Minimal stdlib .env loader.

    Reads KEY=VALUE lines from the .env file next to this module and sets
    them in os.environ WITHOUT overriding variables already present (a
    real exported environment always wins over the file). Returns the
    parsed pairs. A missing file is not an error.
    """
    if path is None:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    pairs: dict[str, str] = {}
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return pairs
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if not key:
            continue
        pairs[key] = val
        os.environ.setdefault(key, val)
    return pairs


_load_dotenv()

_VALID_MODES = ("paper", "testnet", "live")


def _normalize_mode(raw: str) -> str:
    """Fail-closed TRADE_MODE: unknown values fall back to paper."""
    mode = str(raw or "").strip().lower()
    if mode in _VALID_MODES:
        return mode
    print(f"config: unknown TRADE_MODE {raw!r} - falling back to 'paper'",
          file=sys.stderr)
    return "paper"


def _normalize_choice(raw: str, valid: tuple, default: str) -> str:
    """Fail-closed enum knob: unknown values fall back to the default."""
    val = str(raw or "").strip().upper()
    if val in valid:
        return val
    print(f"config: unknown value {raw!r} (expected {'/'.join(valid)})"
          f" - using {default}", file=sys.stderr)
    return default


# ---------------------------------------------------------------------------
# Mode
# ---------------------------------------------------------------------------
# paper  = signals only (no orders)
# testnet = Binance Futures testnet orders
# live   = real USDT-M futures (requires keys + LIVE_CONFIRM=YES)
TRADE_MODE = _normalize_mode(os.environ.get("TRADE_MODE", "testnet"))

# ---------------------------------------------------------------------------
# Binance endpoints
# ---------------------------------------------------------------------------
BINANCE_FAPI = "https://fapi.binance.com"
BINANCE_TESTNET_FAPI = "https://testnet.binancefuture.com"
API_KEY = os.environ.get("BINANCE_API_KEY", "").strip()
API_SECRET = os.environ.get("BINANCE_API_SECRET", "").strip()
LIVE_CONFIRM = os.environ.get("LIVE_CONFIRM", "").strip().upper()

# ---------------------------------------------------------------------------
# Universe
# ---------------------------------------------------------------------------
# comma-separated fixed list, or "top" to take top N by 24h quote volume
UNIVERSE = os.environ.get("UNIVERSE", "top").strip()
TOP_N = int(os.environ.get("TOP_N", "40"))
QUOTE = "USDT"
MIN_24H_VOLUME_USD = float(os.environ.get("MIN_24H_VOLUME_USD", "5_000_000"))

# ---------------------------------------------------------------------------
# Signal engine (bidirectional)
# ---------------------------------------------------------------------------
BULLISH_ONLY = False  # False = LONG + SHORT
MIN_STRENGTH = 3
MIN_QUALITY = int(os.environ.get("MIN_QUALITY", "62"))
SIGNAL_MIN_SCORE = 5
MIN_CANDLES = 80
KLINES_LIMIT = 200

# MTF: hard gate on 1h, weighted score over 1h/30m/15m/5m (user 2026-10-21)
MTF_HARD = ("1h",)
MTF_WEIGHTS = {"1h": 0.40, "30m": 0.25, "15m": 0.20, "5m": 0.15}
MTF_MIN_SCORE = 0.70
BTC_ALIGN = os.environ.get("BTC_ALIGN", "1").strip().lower() not in ("0", "false", "no", "off")

# Noise filters
NOISE_ER_MIN = 0.18
NOISE_DOJI_MAX = 0.60
NOISE_FLIPS_MAX = 3
NOISE_DI_ADX_MIN = 22

# Setup / overextension
OVEREXTENSION_ATR_MAX = 3.0
FRESHNESS_MAX_DRIFT_PCT = 0.6

# Trade plan (ATR multiples)
ATR_PERIOD = 14
SWING_LOOKBACK = 12
ENTRY_ZONE_ATR = 0.10
STOP_ATR_MIN = 1.50
STOP_ATR_MAX = 3.00
ENTRY_MAX_PULLBACK_ATR = 0.50
TP1_RR = 1.50
TP2_RR = 2.25
TP3_RR = 3.00

# ---------------------------------------------------------------------------
# Risk / execution
# ---------------------------------------------------------------------------
RISK_PCT = float(os.environ.get("RISK_PCT", "1.0"))  # % of equity per trade
MAX_LEVERAGE = int(os.environ.get("MAX_LEVERAGE", "5"))
MAX_OPEN_POSITIONS = int(os.environ.get("MAX_OPEN_POSITIONS", "3"))
# Margin mode knob: default ISOLATED (crew standard — loss is contained
# per position). MARGIN_MODE is the primary name; the older MARGIN_TYPE
# env is honoured as a fallback, and MARGIN_TYPE stays as an alias.
MARGIN_MODE = _normalize_choice(
    os.environ.get("MARGIN_MODE", os.environ.get("MARGIN_TYPE", "ISOLATED")),
    ("ISOLATED", "CROSSED"), "ISOLATED")
MARGIN_TYPE = MARGIN_MODE
ORDER_TYPE = _normalize_choice(os.environ.get("ORDER_TYPE", "LIMIT"),
                               ("LIMIT", "MARKET"), "LIMIT")  # LIMIT entry near signal entry; MARKET = immediate
SIGNAL_TTL_SEC = 1800  # ignore signals older than 30 min
SCAN_INTERVAL_SEC = int(os.environ.get("SCAN_INTERVAL_SEC", "900"))  # 15 min
HTTP_TIMEOUT = 15
HTTP_RETRIES = 3

# Entry fill handling: SL/TP are attached ONLY after the entry has filled.
# LIMIT entries are polled for this long; unfilled entries stay "pending"
# in executor state and are reconciled on later trade/loop passes.
ORDER_FILL_WAIT_SEC = int(os.environ.get("ORDER_FILL_WAIT_SEC", "120"))
ORDER_FILL_POLL_SEC = float(os.environ.get("ORDER_FILL_POLL_SEC", "5"))
# Pending LIMIT entries older than this are cancelled at reconciliation.
ORDER_TTL_SEC = int(os.environ.get("ORDER_TTL_SEC", "1800"))
# SL placement: retries before the position is emergency-closed.
SL_ATTACH_RETRIES = int(os.environ.get("SL_ATTACH_RETRIES", "3"))
SL_RETRY_DELAY_SEC = float(os.environ.get("SL_RETRY_DELAY_SEC", "1"))
# Skip entries whose notional (qty x price) is below the exchange
# MIN_NOTIONAL filter (when known) or this floor.
MIN_NOTIONAL_USDT = float(os.environ.get("MIN_NOTIONAL_USDT", "5"))
# Daily loss cap: entries are blocked once equity drops this % below the
# equity recorded at the start of the day (executor state).
DAILY_LOSS_CAP_PCT = float(os.environ.get("DAILY_LOSS_CAP_PCT", "10"))
# Kill switch: if a file named KILL exists in DATA_DIR, no new entries.
KILL_FILE_NAME = "KILL"

# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------
DATA_DIR = os.environ.get("DATA_DIR", "data")
SIGNALS_DB = os.path.join(DATA_DIR, "signals.db")
STATE_FILE = os.path.join(DATA_DIR, "state.json")
LOG_FILE = os.path.join(DATA_DIR, "bot.log")
