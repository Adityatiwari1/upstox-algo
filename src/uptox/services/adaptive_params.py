"""
Adaptive parameter system.

After each trading day, learning_service analyses outcomes and calls
adjust() to nudge parameters toward better performance. Changes are
small and bounded — no wild jumps. Saved to logs/adaptive_params.json
and loaded on startup.
"""
from __future__ import annotations
import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)

_FILE = Path("logs/adaptive_params.json")

# ── Default values ────────────────────────────────────────────────────────────
_DEFAULTS: dict[str, float | int] = {
    "min_score":        2,      # minimum |score| to consider a trade
    "atr_multiplier":   1.5,    # ATR × this = Nifty SL points
    "bk_min_dist":      11.0,   # breakout min departure (pts) before retest
    "bk_timeout":       300,    # seconds to wait for retest
    "sr_tolerance":     15.0,   # Nifty pts — block if within this of S/R
    "pcr_bull_block":   0.5,    # PCR below this → skip CE
    "pcr_bear_block":   2.0,    # PCR above this → skip PE
    "iv_rank_block":    70.0,   # IV rank above this → skip entry
}

# ── Bounds (min, max, step) ───────────────────────────────────────────────────
_BOUNDS: dict[str, tuple] = {
    "min_score":       (2,    4,    1),
    "atr_multiplier":  (1.0,  2.5,  0.1),
    "bk_min_dist":     (8.0,  20.0, 1.0),
    "bk_timeout":      (120,  600,  30),
    "sr_tolerance":    (10.0, 40.0, 5.0),
    "pcr_bull_block":  (0.4,  0.7,  0.05),
    "pcr_bear_block":  (1.5,  2.5,  0.05),
    "iv_rank_block":   (50.0, 85.0, 5.0),
}

_params: dict[str, float | int] = dict(_DEFAULTS)
_change_log: list[dict] = []   # history of what changed and why


def get(key: str) -> float | int:
    return _params.get(key, _DEFAULTS[key])


def get_all() -> dict:
    return dict(_params)


def adjust(key: str, direction: int, reason: str) -> float | int:
    """
    Nudge param by 1 step in direction (+1 or -1).
    Clamped to bounds. Records change.
    """
    if key not in _BOUNDS:
        return _params.get(key, _DEFAULTS.get(key, 0))
    lo, hi, step = _BOUNDS[key]
    old = _params[key]
    new = round(old + direction * step, 4)
    new = max(lo, min(hi, new))
    if new != old:
        _params[key] = new
        entry = {"param": key, "old": old, "new": new, "reason": reason}
        _change_log.append(entry)
        log.info("Adaptive: %s  %.2f → %.2f  (%s)", key, old, new, reason)
    return _params[key]


def reset(key: str) -> None:
    _params[key] = _DEFAULTS[key]


def save() -> None:
    try:
        _FILE.parent.mkdir(exist_ok=True)
        _FILE.write_text(
            json.dumps({"params": _params, "log": _change_log[-50:]},
                       indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception as e:
        log.error("adaptive_params save: %s", e)


def load() -> None:
    global _params, _change_log
    if not _FILE.exists():
        return
    try:
        data     = json.loads(_FILE.read_text(encoding="utf-8"))
        loaded   = data.get("params", {})
        _change_log = data.get("log", [])
        # Only load keys we recognise; ignore stale/unknown keys
        for k, v in loaded.items():
            if k in _DEFAULTS:
                _params[k] = type(_DEFAULTS[k])(v)
        log.info("Adaptive params loaded: %s", _params)
    except Exception as e:
        log.error("adaptive_params load: %s", e)


def get_change_log(n: int = 10) -> list[dict]:
    return _change_log[-n:]


# Load on import
load()
