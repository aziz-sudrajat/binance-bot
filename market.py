"""Binance USDT-M Futures market data + signed trading client."""

from __future__ import annotations

import hashlib
import hmac
import logging
import time
from typing import Any
from urllib.parse import urlencode

import requests

import config

log = logging.getLogger("market")


class BinanceFutures:
    """Public klines/tickers + optional signed trade endpoints."""

    def __init__(self, api_key: str | None = None, api_secret: str | None = None,
                 testnet: bool | None = None):
        if testnet is None:
            testnet = config.TRADE_MODE != "live"
        self.base = config.BINANCE_TESTNET_FAPI if testnet else config.BINANCE_FAPI
        self.api_key = (api_key if api_key is not None else config.API_KEY) or ""
        self.api_secret = (api_secret if api_secret is not None else config.API_SECRET) or ""
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
            # re-sign per attempt: a signed timestamp goes stale across
            # retry sleeps (recvWindow), so reuse can fail with -1021
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

    def _delete(self, path: str, params: dict | None = None, signed: bool = True) -> Any:
        params = dict(params or {})
        if signed:
            params = self._sign(params)
        url = self.base + path
        r = self.session.delete(url, params=params, timeout=config.HTTP_TIMEOUT)
        if r.status_code >= 400:
            raise RuntimeError(f"DELETE {path} {r.status_code}: {r.text[:300]}")
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
            self._exchange_info = self._get("/fapi/v1/exchangeInfo")
        return self._exchange_info

    def list_usdt_perps(self) -> list[str]:
        info = self.exchange_info()
        out = []
        for s in info.get("symbols", []):
            if (s.get("quoteAsset") == config.QUOTE
                    and s.get("contractType") == "PERPETUAL"
                    and s.get("status") == "TRADING"
                    and s.get("symbol", "").endswith(config.QUOTE)):
                out.append(s["symbol"])
        return sorted(out)

    def symbol_filters(self, symbol: str) -> dict:
        """Return tickSize, stepSize, minQty, minNotional for symbol."""
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
                    elif f.get("filterType") == "MIN_NOTIONAL":
                        min_notional = float(f["notional"])
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
        raw = self._get("/fapi/v1/klines", {
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
        # Drop the still-forming last candle: every consumer of klines is
        # signal analysis, and evaluating an unclosed candle repaints
        # (signals appear/disappear intra-candle, volume ratio skewed).
        now_ms = int(time.time() * 1000)
        if out and out[-1]["close_time"] >= now_ms:
            out.pop()
        return out

    def get_ticker_price(self, symbol: str) -> float:
        data = self._get("/fapi/v1/ticker/price", {"symbol": symbol})
        return float(data["price"])

    def get_24h_tickers(self) -> list[dict]:
        return self._get("/fapi/v1/ticker/24hr")

    def top_symbols_by_volume(self, n: int | None = None) -> list[str]:
        n = n or config.TOP_N
        tickers = self.get_24h_tickers()
        rows = []
        for t in tickers:
            sym = t.get("symbol") or ""
            if not sym.endswith(config.QUOTE):
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
            data = self._get("/fapi/v1/ticker/24hr", {"symbol": symbol})
            return float(data.get("quoteVolume") or 0)
        except Exception:  # noqa: BLE001
            return None

    # -------------------------------------------------------------- account
    def balance_usdt(self) -> float:
        data = self._get("/fapi/v2/balance", signed=True)
        for row in data:
            if row.get("asset") == "USDT":
                return float(row.get("availableBalance") or row.get("balance") or 0)
        return 0.0

    def equity_usdt(self) -> float:
        """True account equity (margin balance): wallet + unrealised PnL.

        Unlike balance_usdt() (available balance), this does NOT drop
        when margin is reserved by pending orders or open positions, so
        it is the correct basis for the daily loss cap and risk sizing.
        (Available balance misread reserved margin as a loss: two
        pending LIMIT entries once tripped the 10% daily cap and
        blocked every remaining signal — live testnet evidence
        2026-10-21.)"""
        try:
            acct = self._get("/fapi/v2/account", signed=True)
            val = float(acct.get("totalMarginBalance") or 0)
            if val > 0:
                return val
        except Exception:  # noqa: BLE001
            pass
        wallet = 0.0
        data = self._get("/fapi/v2/balance", signed=True)
        for row in data:
            if row.get("asset") == "USDT":
                wallet = float(row.get("balance") or 0)
                break
        upnl = sum(float(p.get("unrealized") or 0) for p in self.positions())
        return wallet + upnl

    def positions(self) -> list[dict]:
        data = self._get("/fapi/v2/positionRisk", signed=True)
        open_pos = []
        for p in data:
            amt = float(p.get("positionAmt") or 0)
            if abs(amt) > 0:
                open_pos.append({
                    "symbol": p["symbol"],
                    "amount": amt,
                    "entry": float(p.get("entryPrice") or 0),
                    "unrealized": float(p.get("unRealizedProfit") or 0),
                    "leverage": int(float(p.get("leverage") or 1)),
                    "side": "LONG" if amt > 0 else "SHORT",
                })
        return open_pos

    def set_leverage(self, symbol: str, leverage: int) -> None:
        """Set leverage. Raises on failure — the caller must abort the
        entry rather than trade on a stale account leverage."""
        self._post("/fapi/v1/leverage", {
            "symbol": symbol, "leverage": int(leverage),
        })

    def set_margin_type(self, symbol: str, margin_type: str = "ISOLATED") -> None:
        """Set margin type. Raises on failure (caller aborts the entry),
        except Binance -4046 ("already set"), which is benign."""
        try:
            self._post("/fapi/v1/marginType", {
                "symbol": symbol, "marginType": margin_type,
            })
        except Exception as e:  # noqa: BLE001
            if "4046" not in str(e):
                raise

    def open_orders(self, symbol: str) -> list[dict]:
        """Open orders for a symbol (used to avoid duplicate entries)."""
        try:
            data = self._get("/fapi/v1/openOrders", {"symbol": symbol}, signed=True)
            return data if isinstance(data, list) else []
        except Exception as e:  # noqa: BLE001
            log.warning("openOrders %s: %s", symbol, e)
            return []

    def open_algo_orders(self, symbol: str | None = None) -> list[dict]:
        """Open conditional (algo) orders — where SL/TP live since the
        Algo Order API migration. Used to verify protection is in place."""
        params: dict[str, Any] = {}
        if symbol:
            params["symbol"] = symbol
        try:
            data = self._get("/fapi/v1/openAlgoOrders", params, signed=True)
            return data if isinstance(data, list) else []
        except Exception as e:  # noqa: BLE001
            log.warning("openAlgoOrders %s: %s", symbol, e)
            return []

    def place_order(self, symbol: str, side: str, quantity: float,
                    order_type: str = "MARKET", price: float | None = None,
                    reduce_only: bool = False,
                    stop_price: float | None = None,
                    working_type: str = "MARK_PRICE",
                    client_id: str | None = None) -> dict:
        params: dict[str, Any] = {
            "symbol": symbol,
            "side": side,  # BUY / SELL
            "type": order_type,
            "quantity": quantity,
        }
        if order_type in ("STOP_MARKET", "TAKE_PROFIT_MARKET", "STOP",
                          "TAKE_PROFIT", "TRAILING_STOP_MARKET"):
            # Binance migrated conditional orders to the Algo Order API
            # (effective 2025-12-09): /fapi/v1/order rejects them with
            # -4120 "use the Algo Order API endpoints instead" (hit live
            # on testnet 2026-10-21 — every SL attach failed and the
            # executor emergency-closed each position). Route via
            # /fapi/v1/algoOrder: stopPrice -> triggerPrice,
            # newClientOrderId -> newClientAlgoId.
            if stop_price is None:
                raise ValueError(f"{order_type} requires stop_price")
            algo_params: dict[str, Any] = {
                "algoType": "CONDITIONAL",
                "symbol": symbol,
                "side": side,
                "type": order_type,
                "quantity": quantity,
                "triggerPrice": stop_price,
                "workingType": working_type,
                "timeInForce": "GTE_GTC",
            }
            if reduce_only:
                algo_params["reduceOnly"] = "true"
            if client_id:
                algo_params["newClientAlgoId"] = client_id[:36]
            return self._post("/fapi/v1/algoOrder", algo_params)
        if reduce_only:
            params["reduceOnly"] = "true"
        if client_id:
            params["newClientOrderId"] = client_id[:36]
        if order_type == "LIMIT":
            if price is None:
                raise ValueError("LIMIT requires price")
            params["price"] = price
            params["timeInForce"] = "GTC"
        return self._post("/fapi/v1/order", params)

    def get_order(self, symbol: str, client_id: str | None = None,
                  order_id: int | None = None) -> dict:
        """Query one order by clientOrderId or orderId. Raises when the
        order does not exist (Binance -2013) or the request fails."""
        params: dict[str, Any] = {"symbol": symbol}
        if client_id:
            params["origClientOrderId"] = client_id
        if order_id:
            params["orderId"] = order_id
        return self._get("/fapi/v1/order", params, signed=True)

    def cancel_all(self, symbol: str) -> Any:
        return self._delete("/fapi/v1/allOpenOrders", {"symbol": symbol})
