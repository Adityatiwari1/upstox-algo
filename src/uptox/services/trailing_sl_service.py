"""
Trailing stop-loss tracker for a single open options position.

Three modes:
  Milestone  — progressive trail tightening as price moves toward/past target
               Phase 0 (<50% to tgt): initial trail %
               Phase 1 (≥50%):        15% trail, SL floor = entry (breakeven)
               Phase 2 (≥75%):        12% trail, SL floor = p50 price
               Phase 3 (≥100% / tgt): 10% trail, SL floor = p75 price
               Beyond target:         10% trail, SL rises with every new peak

  Hero/Zero  — step SL at price multiples (last 15 mins of expiry day)
"""
from __future__ import annotations
import logging

log = logging.getLogger(__name__)

_entry_price: float = 0.0
_initial_sl:  float = 0.0
_target:      float = 0.0
_side:        str   = ""
_option_key:  str   = ""

_peak:        float = 0.0
_trail_sl:    float = 0.0
_trail_pct:   float = 0.20   # dynamic, changes per milestone
_sl_floor:    float = 0.0    # SL never drops below this

_p50:         float = 0.0    # entry + 50% of range
_p75:         float = 0.0    # entry + 75% of range
_phase:       int   = 0      # 0/1/2/3

_active:      bool  = False
_stopped_out: bool  = False
_hero_zero:   bool  = False

# Hero/Zero step levels (sorted high → low)
_HZ_STEPS = [
    (10.0, 7.0),
    (5.0,  3.5),
    (3.0,  2.0),
    (2.0,  1.0),
]


def _hz_sl(peak: float, entry: float) -> float:
    if not entry:
        return 0.0
    multiple = peak / entry
    for trigger, sl_mult in _HZ_STEPS:
        if multiple >= trigger:
            return round(entry * sl_mult, 1)
    return round(entry * 0.50, 1)


def _milestone_trail_sl(ltp: float) -> float:
    """
    Compute trail SL based on current milestone phase.
    Returns the higher of (peak-based trail) and (milestone SL floor).
    """
    global _phase, _trail_pct, _sl_floor

    if not _entry_price or not _target:
        return round(_peak * (1 - _trail_pct), 1)

    rng      = _target - _entry_price
    progress = (ltp - _entry_price) / rng if rng > 0 else 0.0

    # Phase transitions (only advance, never retreat)
    if _phase < 3 and progress >= 1.0:
        _phase     = 3
        _trail_pct = 0.10
        _sl_floor  = _p75
        log.info("Trail SL phase→3 (target hit): trail=10%%  floor=%.1f", _sl_floor)

    elif _phase < 2 and progress >= 0.75:
        _phase     = 2
        _trail_pct = 0.12
        _sl_floor  = _p50
        log.info("Trail SL phase→2 (75%% of target): trail=12%%  floor=%.1f", _sl_floor)

    elif _phase < 1 and progress >= 0.50:
        _phase     = 1
        _trail_pct = 0.15
        _sl_floor  = _entry_price
        log.info("Trail SL phase→1 (50%% of target): trail=15%%  floor=%.1f (breakeven)", _sl_floor)

    peak_trail = round(_peak * (1 - _trail_pct), 1)
    return max(peak_trail, _sl_floor)


# ── public API ────────────────────────────────────────────────────────────────

def set_entry(
    price: float,
    side: str,
    option_key: str  = "",
    trail_pct: float | None = None,
    target: float    = 0.0,
    initial_sl: float = 0.0,
) -> None:
    """Milestone-based trailing SL. trail_pct is initial %; tightens at each milestone."""
    global _entry_price, _initial_sl, _target, _side, _option_key
    global _peak, _trail_sl, _trail_pct, _sl_floor
    global _p50, _p75, _phase, _active, _stopped_out, _hero_zero

    _entry_price = price
    _initial_sl  = initial_sl if initial_sl else round(price * 0.80, 1)
    _target      = target
    _side        = side
    _option_key  = option_key
    _peak        = price
    _phase       = 0
    _hero_zero   = False
    _active      = True
    _stopped_out = False

    # Initial trail % from actual SL (e.g. SL=90, entry=100 → 10% trail)
    if trail_pct is not None:
        _trail_pct = trail_pct
    elif initial_sl and initial_sl < price:
        _trail_pct = (price - initial_sl) / price
    else:
        _trail_pct = 0.20

    _sl_floor = _initial_sl

    # Pre-compute milestone prices
    rng  = (_target - _entry_price) if _target > _entry_price else 0.0
    _p50 = round(_entry_price + 0.50 * rng, 1) if rng else 0.0
    _p75 = round(_entry_price + 0.75 * rng, 1) if rng else 0.0

    _trail_sl = max(round(_peak * (1 - _trail_pct), 1), _sl_floor)

    log.info(
        "Trail SL: %s %s @ %.1f  SL=%.1f (%.0f%% trail)  tgt=%.1f  p50=%.1f  p75=%.1f",
        side, option_key, price, _trail_sl, _trail_pct * 100, _target, _p50, _p75,
    )


def activate_hero_zero(price: float, side: str, option_key: str = "") -> None:
    """Hero/Zero step-SL mode for last 15 mins of expiry."""
    global _entry_price, _initial_sl, _target, _side, _option_key
    global _peak, _trail_sl, _active, _stopped_out, _hero_zero, _phase

    _entry_price = price
    _initial_sl  = round(price * 0.50, 1)
    _target      = 0.0
    _side        = side
    _option_key  = option_key
    _peak        = price
    _trail_sl    = round(price * 0.50, 1)
    _phase       = 0
    _active      = True
    _stopped_out = False
    _hero_zero   = True
    log.info(
        "Hero/Zero SL: %s %s @ %.1f  initial_SL=%.1f (50%%)",
        side, option_key, price, _trail_sl,
    )


def update(instrument_key: str, ltp: float) -> bool:
    """
    Update peak + SL on each tick. Returns True when SL is triggered for first time.
    """
    global _peak, _trail_sl, _stopped_out

    if not _active or not ltp or not _option_key:
        return False
    if instrument_key != _option_key:
        return False

    if ltp > _peak:
        _peak = ltp

    _trail_sl = (
        _hz_sl(_peak, _entry_price)
        if _hero_zero
        else _milestone_trail_sl(ltp)
    )

    if not _stopped_out and ltp <= _trail_sl:
        _stopped_out = True
        log.warning(
            "SL triggered [%s] phase=%d: ltp=%.1f <= sl=%.1f  entry=%.1f  peak=%.1f",
            "HZ" if _hero_zero else "trail", _phase, ltp, _trail_sl, _entry_price, _peak,
        )
        return True
    return False


def exit_trade() -> None:
    global _active, _stopped_out, _hero_zero, _phase
    _active      = False
    _stopped_out = False
    _hero_zero   = False
    _phase       = 0
    log.info("Trail SL: exited")


def is_active() -> bool:
    return _active

def is_hero_zero() -> bool:
    return _hero_zero


def get_status() -> dict:
    rng      = (_target - _entry_price) if (_target and _entry_price) else 0.0
    progress = round((_peak - _entry_price) / rng * 100, 1) if rng > 0 else 0.0
    return {
        "active":      _active,
        "hero_zero":   _hero_zero,
        "stopped_out": _stopped_out,
        "entry":       _entry_price,
        "target":      _target,
        "side":        _side,
        "peak":        _peak,
        "trail_sl":    _trail_sl,
        "sl_floor":    _sl_floor,
        "trail_pct":   _trail_pct if not _hero_zero else None,
        "phase":       _phase,
        "progress_pct": progress,
        "pnl_pts":     round(_peak - _entry_price, 1) if _active else 0.0,
        "in_profit":   _active and _peak > _entry_price,
        "multiple":    round(_peak / _entry_price, 2) if (_active and _entry_price) else 1.0,
    }
