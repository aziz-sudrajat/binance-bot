"""Binance SPOT market data + signed trading client.

Mirrors the futures client (market.py): same HMAC signing, same kline
shape, same unclosed-candle drop. Differences: REST base is
api.binance.com / testnet.binance.vision (NOT fapi), orders are plain
MARKET (quoteOrderQty for buys), no leverage/margin/algo orders, and
account balances come from /api/v3/account.

Spot keys are separate from futures keys (see config.SPOT_*).
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import time
from typing import Any
from urllib.parse import urlencode

import requests

import config

log = logging.getLogger("spot_market")

_STABLE_BASES = {"USDT", "USDC", "FDUSD", "TUSD", "DAI", "USDP", "BUSD",
                 "EUR", "GBP", "TRY", "BRL", "AUD"}
_LEV_SUFFIXES = ("UP", "DOWN", "BULL", "BEAR")


class SpotClient:
    """Public klines/tickers + optional signed spot trade endpoints."""

    def __init__(self, api_key: str | None = None,
                 api_secret: str | None = None,
                 testnet: bool | None = None):
        if testnet is None:
            testnet = config.SPOT_MODE != "live"
        self.base = config.SPOT_TESTNET_BASE if testnet else config.SPOT_LIVE_BASE
        self.api_key = (api_key if api_key is not None else config.SPOT_API_KEY) or ""
        self.api_secret = (api_secret if api_secret is not None else config.SPOT_API_SECRET) or ""
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "binance-bot/1.0"})
        if self.api_key:
            self.session.headers["X-MBX-APIKEY"] = self.api_key
        self._exchange_info: dict | None = None

    # ------------------------------------------------------------------ HTTP
    def _get(self, path: str, params: dict | None = None, signed: bool = False) -> Any:
        params = dict(params or {})
        url = self.base + path
        last_err = None
        for attempt in range(config.HTTP_RETRIES):
            attempt_params = self._sign(params) if signed else params
            try:
                r = self.session.get(url, params=attempt_params, timeout=config.HTTP_TIMEOUT)
                if r.status_code == 429:
                    last_err = RuntimeError("429 rate limited")
                    time.sleep(1.5 * (attempt + 1))
                    continue
                r.raise_for_status()
                return r.json()
            except Exception as e:  # noqa: BLE001
                last_err = e
                time.sleep(0.4 * (attempt + 1))
        raise RuntimeError(f"GET {path} failed: {last_err}")

    def _post(self, path: str, params: dict | None = None, signed: bool = True) -> Any:
        params = dict(params or {})
        if signed:
            params = self._sign(params)
        url = self.base + path
        r = self.session.post(url, params=params, timeout=config.HTTP_TIMEOUT)
        if r.status_code >= 400:
            raise RuntimeError(f"POST {path} {r.status_code}: {r.text[:300]}")
        return r.json()

    def _sign(self, params: dict) -> dict:
        if not self.api_secret:
            raise RuntimeError("API secret required for signed requests")
        params = dict(params)
        params["timestamp"] = int(time.time() * 1000)
        params.setdefault("recvWindow", 5000)
        query = urlencode(params)
        sig = hmac.new(self.api_secret.encode(), query.encode(),
                       hashlib.sha256).hexdigest()
        params["signature"] = sig
        return params

    # ----------------------------------------------------------- public data
    def exchange_info(self) -> dict:
        if self._exchange_info is None:
            self._exchange_info = self._get("/api/v3/exchangeInfo")
        return self._exchange_info

    def symbol_filters(self, symbol: str) -> dict:
        info = self.exchange_info()
        for s in info.get("symbols", []):
            if s.get("symbol") == symbol:
                tick = step = min_qty = min_notional = None
                for f in s.get("filters", []):
                    if f.get("filterType") == "PRICE_FILTER":
                        tick = float(f["tickSize"])
                    elif f.get("filterType") == "LOT_SIZE":
                        step = float(f["stepSize"])
                        min_qty = float(f["minQty"])
                    elif f.get("filterType") == "NOTIONAL":
                        min_notional = float(f["minNotional"])
                return {"tickSize": tick or 0.01, "stepSize": step or 0.001,
                        "minQty": min_qty or 0.001,
                        "minNotional": min_notional,
                        "pricePrecision": s.get("pricePrecision", 4),
                        "quantityPrecision": s.get("quantityPrecision", 3)}
        return {"tickSize": 0.01, "stepSize": 0.001, "minQty": 0.001,
                "minNotional": None,
                "pricePrecision": 4, "quantityPrecision": 3}

    def get_klines(self, symbol: str, interval: str,
                   limit: int | None = None) -> list[dict]:
        limit = limit or config.KLINES_LIMIT
        raw = self._get("/api/v3/klines", {
            "symbol": symbol, "interval": interval, "limit": limit,
        })
        out = []
        for row in raw:
            out.append({
                "open_time": int(row[0]),
                "open": float(row[1]),
                "high": float(row[2]),
                "low": float(row[3]),
                "close": float(row[4]),
                "volume": float(row[5]),
                "close_time": int(row[6]),
                "quote_volume": float(row[7]),
            })
        # Drop the still-forming last candle (same repaint rule as futures).
        now_ms = int(time.time() * 1000)
        if out and out[-1]["close_time"] >= now_ms:
            out.pop()
        return out

    def get_ticker_price(self, symbol: str) -> float:
        data = self._get("/api/v3/ticker/price", {"symbol": symbol})
        return float(data["price"])

    def get_24h_tickers(self) -> list[dict]:
        return self._get("/api/v3/ticker/24hr")

    def top_symbols_by_volume(self, n: int | None = None) -> list[str]:
        """Top USDT spot pairs by quote volume, excluding stable-base
        pairs and leveraged tokens (UP/DOWN/BULL/BEAR)."""
        n = n or config.SPOT_TOP_N
        tickers = self.get_24h_tickers()
        rows = []
        for t in tickers:
            sym = t.get("symbol") or ""
            if not sym.endswith(config.QUOTE):
                continue
            base = sym[:-len(config.QUOTE)]
            if base in _STABLE_BASES or base.endswith(_LEV_SUFFIXES):
                continue
            try:
                vol = float(t.get("quoteVolume") or 0)
            except (TypeError, ValueError):
                continue
            if vol < config.MIN_24H_VOLUME_USD:
                continue
            rows.append((sym, vol))
        rows.sort(key=lambda x: x[1], reverse=True)
        return [s for s, _ in rows[:n]]

    def get_24h_quote_volume(self, symbol: str) -> float | None:
        try:
            data = self._get("/api/v3/ticker/24hr", {"symbol": symbol})
            return float(data.get("quoteVolume") or 0)
        except Exception:  # noqa: BLE001
            return None

    # -------------------------------------------------------------- account
    def _balances(self) -> dict:
        data = self._get("/api/v3/account", signed=True)
        out = {}
        for row in data.get("balances", []):
            out[row["asset"]] = float(row.get("free") or 0)
        return out

    def usdt_balance(self) -> float:
        return self._balances().get("USDT", 0.0)

    def asset_balance(self, asset: str) -> float:
        return self._balances().get(asset, 0.0)

    # -------------------------------------------------------------- trading
    def buy_market_quote(self, symbol: str, usdt: float) -> dict:
        """MARKET buy spending exactly `usdt` USDT (quoteOrderQty)."""
        return self._post("/api/v3/order", {
            "symbol": symbol, "side": "BUY", "type": "MARKET",
            "quoteOrderQty": f"{usdt:.8f}",
        })

    def sell_market_qty(self, symbol: str, qty: float) -> dict:
        """MARKET sell of `qty` base units."""
        return self._post("/api/v3/order", {
            "symbol": symbol, "side": "SELL", "type": "MARKET",
            "quantity": f"{qty:.8f}",
        })
