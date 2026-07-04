"""
Trailing stop-loss tracker for a single open options position.

Checked every tick (not just bar close) — intrabar spikes lock in profit
immediately instead of waiting for a 5-min candle close.

  Phase 0 (0–33% to tgt):    floor = initial_sl
  Phase 1 (≥33%):            floor = entry            (breakeven)
  Phase 2 (≥50%):            floor = entry + 25%×range
  Phase 3 (≥66%):            floor = entry + 50%×range
  Phase 4 (≥100% / target):  floor = entry + 66%×range — target then extends

Post-target (phase 4+), every time the target extends further:
  anchor = previous target, ext_range = new_target − anchor
  floor steps up by 25%×ext_range every time price advances another 25% of
  ext_range — a constant 25-point lag that repeats on every further extension.

Expiry day uses a separate mechanism (bar-close 35% trail) — unaffected by
the phase system above, since expiry-day option decay/spike behaviour is
fundamentally different from a normal trending day.
"""
from __future__ import annotations
import logging

log = logging.getLogger(__name__)

_entry_price:    float = 0.0
_initial_sl:     float = 0.0
_initial_risk:   float = 0.0   # entry - initial_sl — kept for display/back-compat
_target:         float = 0.0
_side:           str   = ""
_option_key:     str   = ""

_peak:           float = 0.0
_current_price:  float = 0.0   # latest option LTP — used for real-time P&L display
_trail_sl:       float = 0.0
_sl_floor:       float = 0.0   # SL never drops below this

_phase:          int   = 0     # 0/1/2/3/4

_post_target_anchor: float = 0.0   # previous target — anchor for the repeating 25%-lag rule
_post_target_range:  float = 0.0   # latest extension's range (new_target - anchor); 0 = not extended yet

_active:          bool  = False
_stopped_out:     bool  = False
_expiry_afternoon: bool  = False   # per-tick multiple SL active on expiry day
_expiry_day:       bool  = False   # bar-close 35% trail instead of phase system above

# Expiry afternoon per-tick step SL — locks in at price multiples (intrabar spike protection)
_EXPIRY_STEPS = [
    (5.0, 3.5),   # 5× entry → SL at 3.5×
    (3.0, 2.0),   # 3× entry → SL at 2.0×
    (2.0, 1.5),   # 2× entry → SL at 1.5×
]


def _update_phase(ltp: float) -> None:
    """
    Checked every tick. Advances SL floor through milestones 33/50/66/100%,
    then applies the repeating 25%-lag rule once target starts extending.
    Floor only ever increases.
    """
    global _phase, _sl_floor, _post_target_anchor, _post_target_range

    if not _entry_price or not _target:
        return

    rng = _target - _entry_price
    if rng <= 0:
        return
    progress = (ltp - _entry_price) / rng

    if _phase < 4 and progress >= 1.0:
        _phase = 4
        new_floor = round(_entry_price + 0.66 * rng, 1)
        if new_floor > _sl_floor:
            _sl_floor = new_floor
            log.info("Trail SL phase→4 (target hit): floor → %.1f", _sl_floor)
        _post_target_anchor = _target
        _post_target_range  = 0.0   # set by shift_target() when target extends

    elif _phase < 3 and progress >= 0.66:
        _phase = 3
        new_floor = round(_entry_price + 0.50 * rng, 1)
        if new_floor > _sl_floor:
            _sl_floor = new_floor
            log.info("Trail SL phase→3 (66%%): floor → %.1f", _sl_floor)

    elif _phase < 2 and progress >= 0.50:
        _phase = 2
        new_floor = round(_entry_price + 0.25 * rng, 1)
        if new_floor > _sl_floor:
            _sl_floor = new_floor
            log.info("Trail SL phase→2 (50%%): floor → %.1f", _sl_floor)

    elif _phase < 1 and progress >= 0.33:
        _phase = 1
        if _entry_price > _sl_floor:
            _sl_floor = _entry_price
            log.info("Trail SL phase→1 (33%%, breakeven): floor → %.1f", _sl_floor)

    # Repeating 25%-lag rule — normal days only (expiry day uses bar-close 35% trail instead)
    if not _expiry_day and _phase >= 4 and _post_target_range > 0:
        progress2 = (ltp - _post_target_anchor) / _post_target_range
        step = int(progress2 / 0.25)   # 0,1,2,3,4...
        if step >= 1:
            new_floor = round(_post_target_anchor + (step - 1) * 0.25 * _post_target_range, 1)
            if new_floor > _sl_floor:
                _sl_floor = new_floor
                log.info("Post-target trail (25%% lag): floor → %.1f", _sl_floor)


# ── public API ────────────────────────────────────────────────────────────────

def set_entry(
    price: float,
    side: str,
    option_key: str       = "",
    trail_pct: float | None = None,   # noqa: kept for API compat, no longer used
    target: float         = 0.0,
    initial_sl: float     = 0.0,
    expiry_afternoon: bool = False,
    expiry_day: bool      = False,
) -> None:
    """Milestone-based trailing SL, checked every tick.
    On expiry_afternoon: also applies per-tick multiple SL.
    On expiry_day: bar-close 35% trail used instead of the milestone system above."""
    global _entry_price, _initial_sl, _initial_risk, _target, _side, _option_key
    global _peak, _trail_sl, _sl_floor, _phase
    global _post_target_anchor, _post_target_range
    global _active, _stopped_out, _expiry_afternoon, _expiry_day

    _entry_price      = price
    _initial_sl       = initial_sl if initial_sl else round(price * 0.91, 1)
    _target           = target
    _side             = side
    _option_key       = option_key
    _peak             = price
    _phase            = 0
    _active           = True
    _stopped_out      = False
    _expiry_afternoon = expiry_afternoon
    _expiry_day       = expiry_day

    _initial_risk = round(price - _initial_sl, 1)
    _sl_floor     = _initial_sl
    _trail_sl     = _initial_sl
    _post_target_anchor = 0.0
    _post_target_range  = 0.0

    log.info(
        "Trail SL: %s %s @ %.1f  SL=%.1f  tgt=%.1f",
        side, option_key, price, _trail_sl, _target,
    )


def on_bar_close(ltp: float, atr_pts: float | None = None) -> None:
    """
    Expiry-day only: bar-close 35% trail (deep retracements before final surge).
    Normal days: phase milestones are checked per-tick in update() — this is a no-op.
    """
    global _sl_floor
    if not _active or not ltp or not _expiry_day:
        return
    if _phase >= 4:
        trail_dist = round(ltp * 0.35, 1)
        bar_trail  = round(ltp - trail_dist, 1)
        if bar_trail > _sl_floor:
            _sl_floor = bar_trail
            log.info("Expiry post-target trail: bar_close=%.1f  SL → %.1f  [35%%]", ltp, _sl_floor)


def update(instrument_key: str, ltp: float) -> bool:
    """
    Called every tick. Advances phase milestones (non-expiry days), checks
    expiry-afternoon per-tick steps, then checks SL hit against current floor.
    Returns True when SL triggered for first time.
    """
    global _peak, _current_price, _trail_sl, _stopped_out, _sl_floor

    if not _active or not ltp or not _option_key:
        return False
    if instrument_key != _option_key:
        return False

    _current_price = ltp
    if ltp > _peak:
        _peak = ltp

    # Phase milestones — every tick, not just bar close, so intrabar spikes
    # lock in profit immediately. Runs for expiry-day trades too (so phase
    # reaches 4 and on_bar_close's 35% trail can activate); only the
    # repeating 25%-lag extension rule inside is normal-day-only.
    _update_phase(ltp)

    # Expiry afternoon: per-tick multiple SL — catches intrabar spikes that retrace
    # Works alongside the phase/bar-close floor; whichever is higher wins
    if _expiry_afternoon and _entry_price:
        multiple = _peak / _entry_price
        expiry_floor = 0.0
        for trigger, sl_mult in _EXPIRY_STEPS:
            if multiple >= trigger:
                expiry_floor = round(_entry_price * sl_mult, 1)
                break
        if expiry_floor > _sl_floor:
            _sl_floor = expiry_floor
            log.info(
                "Expiry per-tick SL: peak=%.1f (%.1fx entry)  SL floor → %.1f",
                _peak, multiple, _sl_floor,
            )

    _trail_sl = _sl_floor

    if not _stopped_out and ltp <= _trail_sl:
        _stopped_out = True
        log.warning(
            "SL triggered phase=%d: ltp=%.1f <= sl=%.1f  entry=%.1f  peak=%.1f",
            _phase, ltp, _trail_sl, _entry_price, _peak,
        )
        return True
    return False


def shift_target(new_target: float) -> None:
    """
    Extend target forward (called when price blows past current target).
    Anchors the repeating 25%-lag rule at the old target so the next leg
    of the move gets the same step-up trail treatment.
    """
    global _target, _post_target_anchor, _post_target_range
    if not _active or new_target <= _target:
        return
    _post_target_anchor = _target
    _post_target_range  = new_target - _target
    _target = new_target
    log.info(
        "Target shifted → %.1f  anchor=%.1f  ext_range=%.1f",
        _target, _post_target_anchor, _post_target_range,
    )


def set_sl(price: float) -> bool:
    """
    Manual SL override (e.g. from Telegram /sl command). Only allowed to
    tighten — raising the floor for CE-style upward trailing. Never lets the
    SL move backwards, same invariant as the automatic phase system.
    Returns True if applied, False if rejected (no active trade, or price
    would loosen the floor).
    """
    global _sl_floor, _trail_sl
    if not _active or not price:
        return False
    if price <= _sl_floor:
        return False
    _sl_floor = round(price, 1)
    _trail_sl = _sl_floor
    log.info("Trail SL manually set → %.1f", _sl_floor)
    return True


def exit_trade() -> None:
    global _active, _stopped_out, _phase, _expiry_afternoon, _expiry_day
    global _post_target_anchor, _post_target_range
    _active             = False
    _stopped_out        = False
    _phase              = 0
    _expiry_afternoon   = False
    _expiry_day         = False
    _post_target_anchor = 0.0
    _post_target_range  = 0.0
    log.info("Trail SL: exited")


def is_active() -> bool:
    return _active


def get_status() -> dict:
    rng         = (_target - _entry_price) if (_target and _entry_price) else 0.0
    progress    = round((_peak - _entry_price) / rng * 100, 1) if rng > 0 else 0.0
    trail_pct   = round((_peak - _sl_floor) / _peak, 4) if _peak else 0.0
    return {
        "active":           _active,
        "stopped_out":      _stopped_out,
        "expiry_afternoon": _expiry_afternoon,
        "expiry_day":       _expiry_day,
        "entry":            _entry_price,
        "target":           _target,
        "side":             _side,
        "peak":             _peak,
        "trail_sl":         _trail_sl,
        "sl_floor":         _sl_floor,
        "trail_pct":        trail_pct,
        "phase":            _phase,
        "progress_pct":     progress,
        "pnl_pts":          round(_current_price - _entry_price, 1) if (_active and _current_price) else 0.0,
        "peak_pnl_pts":     round(_peak - _entry_price, 1) if _active else 0.0,
        "in_profit":        _active and _current_price > _entry_price,
        "multiple":         round(_peak / _entry_price, 2) if (_active and _entry_price) else 1.0,
    }
