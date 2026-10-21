#!/usr/bin/env python3
"""binance-bot — simple LONG+SHORT signal scanner + auto-trader.

Usage:
  python main.py scan              # one scan pass
  python main.py trade             # open trades from fresh signals
  python main.py run               # scan + trade once
  python main.py loop              # continuous scan+trade
  python main.py signals           # show fresh signals
  python main.py positions         # show open positions
  python main.py balance           # show USDT balance
  python main.py settings          # show runtime settings
  python main.py set <key> <value> # change a setting (applies next cycle)
  python main.py spot-scan         # one spot swing scan pass
  python main.py spot-trade        # buy spot from fresh signals
  python main.py spot-loop         # continuous spot scan+trade
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time

import config
import settings
from executor import Executor
from market import BinanceFutures
from scanner import SignalStore, ensure_data_dir, run_scan


def setup_logging() -> None:
    ensure_data_dir()
    level = logging.INFO
    fmt = "%(asctime)s %(levelname)s %(name)s: %(message)s"
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    try:
        handlers.append(logging.FileHandler(config.LOG_FILE))
    except OSError:
        pass
    logging.basicConfig(level=level, format=fmt, handlers=handlers)


def cmd_scan(_args) -> int:
    stats = run_scan()
    print(json.dumps({k: v for k, v in stats.items() if k != "signals"}, indent=2))
    if stats.get("signals"):
        print("\nApproved:")
        for s in stats["signals"]:
            print(f"  {s['direction']:5} {s['symbol']:12} Q={s['quality']} entry={s['entry']}")
    return 0


def cmd_trade(_args) -> int:
    ex = Executor()
    try:
        recs = ex.process_fresh_signals()
    finally:
        ex.close()
    print(json.dumps(recs, indent=2, default=str))
    return 0


def cmd_run(_args) -> int:
    run_scan()
    ex = Executor()
    try:
        recs = ex.process_fresh_signals()
    finally:
        ex.close()
    print(f"opened {len(recs)} trades")
    return 0


def cmd_loop(args) -> int:
    log = logging.getLogger("main")
    log.info("loop every %ss mode=%s",
             args.interval or config.SCAN_INTERVAL_SEC, config.TRADE_MODE)
    ex = Executor()
    try:
        while True:
            try:
                run_scan()
                ex.process_fresh_signals()
            except KeyboardInterrupt:
                log.info("stopped")
                return 0
            except Exception as e:  # noqa: BLE001
                log.exception("loop error: %s", e)
            # re-read each cycle so scan_interval_sec setting changes
            # take effect without a restart
            time.sleep(args.interval or config.SCAN_INTERVAL_SEC)
    finally:
        ex.close()


def cmd_signals(_args) -> int:
    store = SignalStore()
    try:
        rows = store.fresh(30)
    finally:
        store.close()
    if not rows:
        print("no fresh signals")
        return 0
    for s in rows:
        print(f"{s['direction']:5} {s['symbol']:12} Q={s['quality']:3} "
              f"entry={s['entry']:.6g} stop={s['stop_loss']:.6g} "
              f"tp1={s['tp1']:.6g} regime={s.get('regime')}")
    return 0


def cmd_positions(_args) -> int:
    m = BinanceFutures()
    if not m.api_key:
        print("set BINANCE_API_KEY to query positions")
        return 1
    for p in m.positions():
        print(f"{p['side']:5} {p['symbol']:12} amt={p['amount']} "
              f"entry={p['entry']} uPnL={p['unrealized']}")
    return 0


def cmd_balance(_args) -> int:
    m = BinanceFutures()
    if not m.api_key:
        print("set BINANCE_API_KEY")
        return 1
    print(f"USDT available: {m.balance_usdt():.4f}")
    print(f"mode={config.TRADE_MODE} testnet={config.TRADE_MODE != 'live'}")
    return 0


def cmd_spot_scan(_args) -> int:
    from spot_engine import run_spot_scan

    stats = run_spot_scan()
    print(json.dumps({k: v for k, v in stats.items() if k != "signals"}, indent=2))
    if stats.get("signals"):
        print("\nSpot approved:")
        for s in stats["signals"]:
            print(f"  LONG {s['symbol']:12} Q={s['quality']} entry={s['entry']} "
                  f"tp1={s['tp1']:.6g} structure={s.get('structure')} "
                  f"patterns={s.get('patterns')}")
    return 0


def cmd_spot_trade(_args) -> int:
    from spot_executor import SpotExecutor

    ex = SpotExecutor()
    try:
        recs = ex.process_spot_signals()
    finally:
        ex.close()
    print(json.dumps(recs, indent=2, default=str))
    return 0


def cmd_spot_loop(args) -> int:
    from spot_engine import run_spot_scan
    from spot_executor import SpotExecutor

    log = logging.getLogger("main")
    if not config.SPOT_API_KEY:
        log.warning("no SPOT_API_KEY — spot loop runs scan-only until "
                    "spot keys are provided")
    log.info("spot loop every %ss mode=%s",
             args.interval or config.SCAN_INTERVAL_SEC, config.SPOT_MODE)
    ex = SpotExecutor()
    try:
        while True:
            try:
                run_spot_scan()
                ex.process_spot_signals()
            except KeyboardInterrupt:
                log.info("spot loop stopped")
                return 0
            except Exception as e:  # noqa: BLE001
                log.exception("spot loop error: %s", e)
            time.sleep(args.interval or config.SCAN_INTERVAL_SEC)
    finally:
        ex.close()


def cmd_settings(_args) -> int:
    eff = settings.effective()
    print(f"settings file: {settings.settings_path()}")
    print("changes apply on the next loop cycle (no restart needed).")
    for key in sorted(eff):
        print(f"  {key} = {eff[key]}")
    return 0


def cmd_set(args) -> int:
    try:
        eff = settings.set_value(args.key, args.value)
    except ValueError as e:
        print(f"error: {e}")
        return 2
    print(f"{args.key} = {eff[args.key]} (applies next cycle)")
    return 0


def main() -> int:
    setup_logging()
    p = argparse.ArgumentParser(description="binance-bot LONG+SHORT futures")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("scan", help="run one signal scan")
    sub.add_parser("trade", help="auto-trade fresh signals")
    sub.add_parser("run", help="scan then trade once")
    lp = sub.add_parser("loop", help="continuous scan+trade")
    lp.add_argument("--interval", type=int, default=None)
    sub.add_parser("signals", help="list fresh signals")
    sub.add_parser("positions", help="list open positions")
    sub.add_parser("balance", help="show USDT balance")
    sub.add_parser("settings", help="show runtime settings")
    sub.add_parser("spot-scan", help="run one spot swing scan")
    sub.add_parser("spot-trade", help="buy spot from fresh signals")
    sl = sub.add_parser("spot-loop", help="continuous spot scan+trade")
    sl.add_argument("--interval", type=int, default=None)
    sp = sub.add_parser("set", help="change a runtime setting")
    sp.add_argument("key", help="setting name (see: settings)")
    sp.add_argument("value", help="new value")

    args = p.parse_args()
    cmds = {
        "scan": cmd_scan,
        "trade": cmd_trade,
        "run": cmd_run,
        "loop": cmd_loop,
        "signals": cmd_signals,
        "positions": cmd_positions,
        "balance": cmd_balance,
        "settings": cmd_settings,
        "set": cmd_set,
        "spot-scan": cmd_spot_scan,
        "spot-trade": cmd_spot_trade,
        "spot-loop": cmd_spot_loop,
    }
    return cmds[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
