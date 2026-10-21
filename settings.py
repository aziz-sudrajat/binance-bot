"""Runtime settings for binance-bot — change knobs without restarting.

``settings.json`` lives in DATA_DIR. The loop reloads it every cycle
(``Executor.process_fresh_signals`` applies it); the ``set`` CLI writes
it. Env vars remain the base defaults; the file overrides them per key.

margin_mode is LOCKED to ISOLATED (user requirement): any attempt to
store another value is rejected here, and the executor refuses to open
on a non-ISOLATED mode as a second layer.
"""

from __future__ import annotations

import json
import logging
import os

import config

log = logging.getLogger("settings")

FILE_NAME = "settings.json"

# key -> (config attribute, kind, low, high)
# kind "bool" ignores low/high; kind "str" only allows the listed value.
_SPECS = {
    "leverage": ("MAX_LEVERAGE", "int", 1, 125),
    "tp_roi_pct": ("PROFIT_TAKE_PCT", "float", 0, 500),
    "margin_usd": ("FIXED_MARGIN_USD", "float", 0, 1_000_000),
    "margin_mode": ("MARGIN_MODE", "str", None, None),
    "max_positions": ("MAX_OPEN_POSITIONS", "int", 1, 20),
    "min_quality": ("MIN_QUALITY", "int", 0, 100),
    "risk_pct": ("RISK_PCT", "float", 0.1, 100),
    "daily_loss_cap_pct": ("DAILY_LOSS_CAP_PCT", "float", 0, 100),
    "btc_align": ("BTC_ALIGN", "bool", None, None),
    "scan_interval_sec": ("SCAN_INTERVAL_SEC", "int", 60, 86400),
    # spot engine knobs (dual-engine 2026-10-21)
    "spot_trade_usd": ("SPOT_TRADE_USDT", "float", 1, 1_000_000),
    "spot_tp1_atr": ("SPOT_TP1_ATR", "float", 0.5, 20),
    "spot_tp2_atr": ("SPOT_TP2_ATR", "float", 0.5, 40),
    "spot_min_quality": ("SPOT_MIN_QUALITY", "int", 0, 100),
    # kill_switch is special: it drives the KILL file, not a config attr.
    "kill_switch": (None, "bool", None, None),
}

_BOOL_TRUE = {"1", "true", "on", "yes", "y"}
_BOOL_FALSE = {"0", "false", "off", "no", "n"}


def settings_path() -> str:
    return os.path.join(config.DATA_DIR, FILE_NAME)


def _defaults() -> dict:
    out = {}
    for key, (attr, _kind, _lo, _hi) in _SPECS.items():
        if attr is not None:
            out[key] = getattr(config, attr)
    return out


def _coerce(key: str, raw) -> object:
    """Validate + normalize one value. Raises ValueError on bad input."""
    if key not in _SPECS:
        raise ValueError(f"unknown setting: {key} "
                         f"(known: {', '.join(sorted(_SPECS))})")
    _attr, kind, lo, hi = _SPECS[key]
    if key == "margin_mode":
        v = str(raw).strip().upper()
        if v != "ISOLATED":
            raise ValueError("margin_mode is locked to ISOLATED "
                             "(CROSS is forbidden)")
        return "ISOLATED"
    if kind == "bool":
        if isinstance(raw, bool):
            return raw
        s = str(raw).strip().lower()
        if s in _BOOL_TRUE:
            return True
        if s in _BOOL_FALSE:
            return False
        raise ValueError(f"{key}: expected true/false, got {raw!r}")
    try:
        v = int(raw) if kind == "int" else float(raw)
    except (TypeError, ValueError):
        raise ValueError(f"{key}: expected {kind}, got {raw!r}") from None
    if lo is not None and v < lo:
        raise ValueError(f"{key}: {v} below minimum {lo}")
    if hi is not None and v > hi:
        raise ValueError(f"{key}: {v} above maximum {hi}")
    return v


def load_file() -> dict:
    try:
        with open(settings_path(), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def effective() -> dict:
    """Defaults (env) overlaid with valid file values."""
    out = _defaults()
    for key, raw in load_file().items():
        if key == "kill_switch":
            try:
                out[key] = _coerce(key, raw)
            except ValueError:
                continue
            continue
        if key not in _SPECS:
            continue
        try:
            out[key] = _coerce(key, raw)
        except ValueError:
            continue
    # kill switch state also reflects the KILL file when never set
    if "kill_switch" not in out:
        out["kill_switch"] = os.path.exists(
            os.path.join(config.DATA_DIR, config.KILL_FILE_NAME))
    return out


def set_value(key: str, raw) -> dict:
    """Validate, persist one key, and return the new effective settings."""
    value = _coerce(key, raw)
    data = load_file()
    data[key] = value
    with open(settings_path(), "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    return effective()


_LAST_APPLIED: dict | None = None


def apply_to_config() -> bool:
    """Apply effective settings to the config module. Returns True when
    something changed since the last call (the loop logs on change)."""
    global _LAST_APPLIED
    eff = effective()
    changed = eff != _LAST_APPLIED
    for key, (attr, _kind, _lo, _hi) in _SPECS.items():
        if attr is not None and key in eff:
            setattr(config, attr, eff[key])
    if "kill_switch" in load_file():
        kill_path = os.path.join(config.DATA_DIR, config.KILL_FILE_NAME)
        if eff.get("kill_switch"):
            open(kill_path, "a").close()
        elif os.path.exists(kill_path):
            os.remove(kill_path)
    if changed:
        _LAST_APPLIED = eff
        log.info("settings applied: %s",
                 ", ".join(f"{k}={v}" for k, v in sorted(eff.items())))
    return changed
