"""
Telegram notification toggles.
Lets you enable/disable each alert type (and tune sub-settings like
interval/threshold) without touching code. Persisted to
logs/notification_settings.json, loaded on startup.
"""
from __future__ import annotations
import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)

_FILE = Path("logs/notification_settings.json")

# ── Defaults ───────────────────────────────────────────────────────────────────
# Each entry: enabled flag + any sub-settings for that notification type.
_DEFAULTS: dict[str, dict] = {
    "trade_open":         {"enabled": True},
    "trade_close":        {"enabled": True},
    "partial_exit":       {"enabled": True},
    "trade_update":       {"enabled": True,  "interval_min": 15},
    "market_update":      {"enabled": True,  "interval_min": 30},
    "daily_report":       {"enabled": True},
    "weekly_report":      {"enabled": True},
    "monthly_report":     {"enabled": True},
    "monthly_pdf":        {"enabled": True},
    "daily_limit_hit":    {"enabled": True},
    "approaching_limit":  {"enabled": True,  "threshold_pct": 75.0},
    "feed_status":        {"enabled": True},
}

# Bounds for tunable sub-settings: (min, max, step)
# interval_min allows 0 = "None" (periodic notification fully off, independent of enabled switch)
_BOUNDS: dict[str, tuple] = {
    "trade_update.interval_min":      (0,    60,   5),
    "market_update.interval_min":     (0,    60,   5),
    "approaching_limit.threshold_pct": (50.0, 95.0, 5.0),
}

_settings: dict[str, dict] = {k: dict(v) for k, v in _DEFAULTS.items()}


def is_enabled(key: str) -> bool:
    return bool(_settings.get(key, _DEFAULTS.get(key, {})).get("enabled", True))


def set_enabled(key: str, enabled: bool) -> None:
    if key not in _settings:
        return
    _settings[key]["enabled"] = enabled
    log.info("Notification %s -> %s", key, "ON" if enabled else "OFF")


def get_sub(key: str, sub_key: str):
    return _settings.get(key, {}).get(sub_key, _DEFAULTS.get(key, {}).get(sub_key))


def set_sub(key: str, sub_key: str, value) -> None:
    bound_key = f"{key}.{sub_key}"
    if bound_key in _BOUNDS:
        lo, hi, _ = _BOUNDS[bound_key]
        value = max(lo, min(hi, value))
    if key in _settings:
        _settings[key][sub_key] = value
        log.info("Notification %s.%s -> %s", key, sub_key, value)


def get_all() -> dict:
    return {k: dict(v) for k, v in _settings.items()}


def get_defaults(key: str) -> dict:
    return dict(_DEFAULTS.get(key, {}))


def reset_all() -> None:
    global _settings
    _settings = {k: dict(v) for k, v in _DEFAULTS.items()}


def get_bounds(key: str, sub_key: str) -> tuple | None:
    return _BOUNDS.get(f"{key}.{sub_key}")


def save() -> None:
    try:
        _FILE.parent.mkdir(exist_ok=True)
        _FILE.write_text(json.dumps(_settings, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        log.error("notification_settings save: %s", e)


def load() -> None:
    global _settings
    if not _FILE.exists():
        return
    try:
        data = json.loads(_FILE.read_text(encoding="utf-8"))
        for k, v in data.items():
            if k in _DEFAULTS and isinstance(v, dict):
                _settings[k].update(v)
        log.info("Notification settings loaded: %s", _settings)
    except Exception as e:
        log.error("notification_settings load: %s", e)


# Load on import
load()
