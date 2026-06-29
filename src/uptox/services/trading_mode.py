"""
Trading mode — paper vs live.
Persisted to logs/trading_mode.json so restarts remember the setting.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)

_MODE_FILE = Path("logs/trading_mode.json")
_VALID = ("paper", "live")

_mode: str = "paper"


def get_mode() -> str:
    return _mode


def is_live() -> bool:
    return _mode == "live"


def is_paper() -> bool:
    return _mode == "paper"


def set_mode(mode: str) -> None:
    global _mode
    if mode not in _VALID:
        raise ValueError(f"Invalid mode '{mode}'. Must be one of {_VALID}")
    _mode = mode
    _save()
    log.info("Trading mode → %s", mode.upper())


def toggle() -> str:
    """Toggle between paper and live. Returns the new mode."""
    new = "live" if _mode == "paper" else "paper"
    set_mode(new)
    return new


# ── persistence ───────────────────────────────────────────────────────────────

def _save() -> None:
    try:
        _MODE_FILE.parent.mkdir(exist_ok=True)
        _MODE_FILE.write_text(
            json.dumps({"mode": _mode}, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception as e:
        log.error("trading_mode save error: %s", e)


def _load() -> None:
    global _mode
    if _MODE_FILE.exists():
        try:
            data = json.loads(_MODE_FILE.read_text(encoding="utf-8"))
            m = data.get("mode", "paper")
            if m in _VALID:
                _mode = m
                log.info("Trading mode loaded: %s", _mode.upper())
        except Exception as e:
            log.error("trading_mode load error: %s", e)


_load()
