"""Universe scanner — evaluate symbols, store approved signals."""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from collections import Counter

import config
from market import BinanceFutures
from signal_engine import evaluate_symbol

log = logging.getLogger("scanner")


def ensure_data_dir() -> None:
    os.makedirs(config.DATA_DIR, exist_ok=True)


class SignalStore:
    def __init__(self, path: str | None = None):
        ensure_data_dir()
        self.path = path or config.SIGNALS_DB
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        # multi-process safety: scanner and trader may hold the DB at once
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS signals (
                id TEXT PRIMARY KEY,
                symbol TEXT,
                direction TEXT,
                quality INTEGER,
                payload TEXT,
                created_at INTEGER,
                expires_at INTEGER,
                status TEXT DEFAULT 'fresh'
            )"""
        )
        self.conn.commit()

    def upsert(self, signal: dict, ttl_sec: int | None = None) -> str:
        ttl = ttl_sec or config.SIGNAL_TTL_SEC
        now = int(time.time() * 1000)
        sid = f"{signal['symbol']}:{signal['direction']}:{signal.get('timestamp', now)}"
        # a newer plan for the same symbol+direction supersedes older
        # still-fresh rows, so the executor never works a stale duplicate
        self.conn.execute(
            "UPDATE signals SET status='superseded' "
            "WHERE symbol=? AND direction=? AND status='fresh' AND id != ?",
            (signal["symbol"], signal["direction"], sid),
        )
        self.conn.execute(
            """INSERT OR REPLACE INTO signals
               (id, symbol, direction, quality, payload, created_at, expires_at, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'fresh')""",
            (sid, signal["symbol"], signal["direction"], int(signal["quality"]),
             json.dumps(signal), now, now + ttl * 1000),
        )
        self.conn.commit()
        return sid

    def fresh(self, limit: int = 20) -> list[dict]:
        now = int(time.time() * 1000)
        rows = self.conn.execute(
            """SELECT id, payload FROM signals
               WHERE status='fresh' AND expires_at > ?
               ORDER BY quality DESC, created_at DESC LIMIT ?""",
            (now, limit),
        ).fetchall()
        out = []
        for r in rows:
            sig = json.loads(r["payload"])
            sig["_sid"] = r["id"]
            out.append(sig)
        return out

    def mark(self, signal_id: str, status: str) -> None:
        self.conn.execute("UPDATE signals SET status=? WHERE id=?", (status, signal_id))
        self.conn.commit()

    def claim(self, signal_id: str) -> bool:
        """Atomically flip a fresh signal to 'claimed'.

        Returns True only for the caller that won the flip, so two
        executor processes can never open the same signal twice
        (closes the check-then-place TOCTOU window). Expired signals
        cannot be claimed.
        """
        now = int(time.time() * 1000)
        cur = self.conn.execute(
            "UPDATE signals SET status='claimed' "
            "WHERE id=? AND status='fresh' AND expires_at > ?",
            (signal_id, now),
        )
        self.conn.commit()
        return cur.rowcount == 1

    def release_claim(self, signal_id: str) -> None:
        """Give a claimed signal back (executor aborted before ordering)."""
        self.conn.execute(
            "UPDATE signals SET status='fresh' WHERE id=? AND status='claimed'",
            (signal_id,),
        )
        self.conn.commit()

    def expire_old(self) -> int:
        now = int(time.time() * 1000)
        cur = self.conn.execute(
            "UPDATE signals SET status='expired' "
            "WHERE status IN ('fresh', 'claimed') AND expires_at <= ?",
            (now,),
        )
        self.conn.commit()
        return cur.rowcount

    def close(self) -> None:
        self.conn.close()


def resolve_universe(market: BinanceFutures) -> list[str]:
    raw = config.UNIVERSE
    if raw.lower() == "top":
        return market.top_symbols_by_volume(config.TOP_N)
    symbols = [s.strip().upper() for s in raw.split(",") if s.strip()]
    return symbols


def classify_reason(reason: str) -> str:
    r = (reason or "").lower()
    for key in ("volume", "15m", "regime", "noise", "mtf", "btc", "freshness",
                "quality", "strength", "bullish-only", "setup"):
        if key in r:
            return key if key != "setup" else "setup_15m"
    return "other"


def run_scan(market: BinanceFutures | None = None,
             store: SignalStore | None = None) -> dict:
    market = market or BinanceFutures()
    own_store = store is None
    store = store or SignalStore()
    t0 = time.time()
    symbols = resolve_universe(market)
    log.info("scanning %d symbols (mode=%s bullish_only=%s)",
             len(symbols), config.TRADE_MODE, config.BULLISH_ONLY)

    approved = rejected = errors = 0
    funnel: Counter = Counter()
    signals: list[dict] = []

    for i, sym in enumerate(symbols):
        try:
            sig, note = evaluate_symbol(sym, market)
            if sig is None:
                rejected += 1
                funnel[classify_reason(note)] += 1
                log.debug("REJECT %s: %s", sym, note)
            else:
                store.upsert(sig)
                approved += 1
                signals.append(sig)
                log.info("APPROVED %s %s Q=%s — %s",
                         sig["direction"], sym, sig["quality"], note)
        except Exception as e:  # noqa: BLE001
            errors += 1
            funnel["error"] += 1
            log.warning("ERROR %s: %s", sym, e)
        if (i + 1) % 20 == 0:
            log.info("progress %d/%d", i + 1, len(symbols))
        time.sleep(0.12)  # soft rate limit

    expired = store.expire_old()
    stats = {
        "at": int(time.time() * 1000),
        "symbols": len(symbols),
        "approved": approved,
        "rejected": rejected,
        "errors": errors,
        "expired": expired,
        "reject_funnel": dict(funnel.most_common()),
        "duration_s": round(time.time() - t0, 1),
        "signals": [
            {"symbol": s["symbol"], "direction": s["direction"],
             "quality": s["quality"], "entry": s["entry"]}
            for s in signals
        ],
    }
    ensure_data_dir()
    with open(config.STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(stats, f, indent=2)
    log.info("scan done: approved=%d rejected=%d errors=%d funnel=%s",
             approved, rejected, errors, stats["reject_funnel"])
    if own_store:
        store.close()
    return stats
