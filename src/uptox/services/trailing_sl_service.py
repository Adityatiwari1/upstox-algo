"""
Trailing stop-loss tracker for a single open options position.

Two modes:
  Normal     — % trail below peak (set_entry)
  Hero/Zero  — step SL at price multiples (activate_hero_zero)
               Used in last 15 mins of expiry day where options
               can go 10x or zero; % trail is meaningless here.
"""
from __future__ import annotations
import logging

log = logging.getLogger(__name__)

_entry_price: float = 0.0
_side: str          = ""
_option_key: str    = ""
_peak: float        = 0.0
_trail_sl: float    = 0.0
_trail_pct: float   = 0.20
_active: bool       = False
_stopped_out: bool  = False
_hero_zero: bool    = False   # True = step-SL mode

# Hero/Zero step levels (sorted high → low, checked in order)
# (price_multiple_to_trigger, sl_multiple_of_entry)
_HZ_STEPS = [
    (10.0, 7.0),   # price = 10x entry → SL at 7x  (lock 600% gain)
    (5.0,  3.5),   # price = 5x        → SL at 3.5x (lock 250% gain)
    (3.0,  2.0),   # price = 3x        → SL at 2x   (lock 100% gain)
    (2.0,  1.0),   # price = 2x        → SL at 1x   (breakeven)
]


def _hz_sl(peak: float, entry: float) -> float:
    """Compute step SL for Hero/Zero based on peak/entry multiple."""
    if not entry:
        return 0.0
    multiple = peak / entry
    for trigger, sl_mult in _HZ_STEPS:
        if multiple >= trigger:
            return round(entry * sl_mult, 1)
    return round(entry * 0.50, 1)   # below 2x → initial 50% SL


# ── public API ────────────────────────────────────────────────────────────────

def set_entry(price: float, side: str, option_key: str = "",
              trail_pct: float | None = None) -> None:
    """Normal % trailing SL. trail_pct computed from actual SL level."""
    global _entry_price, _side, _option_key, _peak, _trail_sl
    global _trail_pct, _active, _stopped_out, _hero_zero
    _entry_price = price
    _side        = side
    _option_key  = option_key
    _peak        = price
    _trail_pct   = trail_pct if trail_pct is not None else _trail_pct
    _trail_sl    = round(price * (1 - _trail_pct), 1)
    _active      = True
    _stopped_out = False
    _hero_zero   = False
    log.info("Trail SL: %s %s @ %.1f  SL=%.1f (%.0f%% trail)",
             side, option_key, price, _trail_sl, _trail_pct * 100)


def activate_hero_zero(price: float, side: str, option_key: str = "") -> None:
    """
    Hero/Zero step-SL mode for last 15 mins of expiry.
    50% initial SL, then locks profits in steps as price multiplies.
    """
    global _entry_price, _side, _option_key, _peak, _trail_sl
    global _active, _stopped_out, _hero_zero
    _entry_price = price
    _side        = side
    _option_key  = option_key
    _peak        = price
    _trail_sl    = round(price * 0.50, 1)   # 50% initial SL
    _active      = True
    _stopped_out = False
    _hero_zero   = True
    log.info(
        "Hero/Zero SL: %s %s @ %.1f  initial_SL=%.1f (50%%)",
        side, option_key, price, _trail_sl,
    )


def update(instrument_key: str, ltp: float) -> bool:
    """
    Update peak + SL on each tick for the tracked option.
    Returns True the first time ltp drops to/below SL.
    """
    global _peak, _trail_sl, _stopped_out
    if not _active or not ltp or not _option_key:
        return False
    if instrument_key != _option_key:
        return False
    if ltp > _peak:
        _peak     = ltp
        _trail_sl = (_hz_sl(_peak, _entry_price) if _hero_zero
                     else round(_peak * (1 - _trail_pct), 1))
    if not _stopped_out and ltp <= _trail_sl:
        _stopped_out = True
        log.warning(
            "SL triggered [%s]: ltp=%.1f <= sl=%.1f  entry=%.1f  peak=%.1f",
            "HZ" if _hero_zero else "trail", ltp, _trail_sl, _entry_price, _peak,
        )
        return True
    return False


def exit_trade() -> None:
    global _active, _stopped_out, _hero_zero
    _active      = False
    _stopped_out = False
    _hero_zero   = False
    log.info("Trail SL: exited")


def is_active() -> bool:
    return _active


def is_hero_zero() -> bool:
    return _hero_zero


def get_status() -> dict:
    return {
        "active":      _active,
        "hero_zero":   _hero_zero,
        "stopped_out": _stopped_out,
        "entry":       _entry_price,
        "side":        _side,
        "peak":        _peak,
        "trail_sl":    _trail_sl,
        "trail_pct":   _trail_pct if not _hero_zero else None,
        "pnl_pts":     round(_peak - _entry_price, 1) if _active else 0.0,
        "in_profit":   _active and _peak > _entry_price,
        "multiple":    round(_peak / _entry_price, 2) if (_active and _entry_price) else 1.0,
    }
