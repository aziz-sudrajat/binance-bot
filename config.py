"""binance-bot configuration — LONG + SHORT futures signal + auto-trade."""

from __future__ import annotations

import os

# ---------------------------------------------------------------------------
# Mode
# ---------------------------------------------------------------------------
# paper  = signals only (no orders)
# testnet = Binance Futures testnet orders
# live   = real USDT-M futures (requires keys + LIVE_CONFIRM=YES)
TRADE_MODE = os.environ.get("TRADE_MODE", "testnet").strip().lower()

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

# MTF: hard gate on 4h+1h, soft score on lower TFs
MTF_HARD = ("4h", "1h")
MTF_WEIGHTS = {"4h": 0.30, "1h": 0.25, "30m": 0.20, "15m": 0.15, "5m": 0.10}
MTF_MIN_SCORE = 0.70
BTC_ALIGN = os.environ.get("BTC_ALIGN", "1") not in ("0", "false", "False")

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
MARGIN_TYPE = "CROSSED"  # or ISOLATED
ORDER_TYPE = "LIMIT"  # LIMIT entry near signal entry; fallback MARKET
SIGNAL_TTL_SEC = 1800  # ignore signals older than 30 min
SCAN_INTERVAL_SEC = int(os.environ.get("SCAN_INTERVAL_SEC", "900"))  # 15 min
HTTP_TIMEOUT = 15
HTTP_RETRIES = 3

# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------
DATA_DIR = os.environ.get("DATA_DIR", "data")
SIGNALS_DB = os.path.join(DATA_DIR, "signals.db")
STATE_FILE = os.path.join(DATA_DIR, "state.json")
LOG_FILE = os.path.join(DATA_DIR, "bot.log")
