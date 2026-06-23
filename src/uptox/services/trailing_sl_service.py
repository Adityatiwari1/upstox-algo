"""
Trailing stop-loss tracker for a single open options position.

Step-floor SL advances only on 5-min bar close (not per tick) to avoid noise shakeouts.

  Phase 0 (0–33% to tgt):   SL = initial_sl  (fixed)
  Phase 1 (≥33%):           SL = entry        (breakeven) + half-R bar trail
  Phase 2 (≥66%):           SL = entry + 25%×range + half-R bar trail
  Phase 3 (≥100% / target): SL = entry + 50%×range + 1R bar trail → target extends
"""
from __future__ import annotations
import logging

log = logging.getLogger(__name__)

_entry_price:    float = 0.0
_initial_sl:     float = 0.0
_initial_risk:   float = 0.0   # entry - initial_sl — used for post-target bar trailing
_half_risk:      float = 0.0   # initial_risk / 2 — used for post-phase1 bar trailing
_target:         float = 0.0
_side:           str   = ""
_option_key:     str   = ""

_peak:           float = 0.0
_trail_sl:       float = 0.0
_trail_pct:      float = 0.20
_sl_floor:       float = 0.0   # SL never drops below this

_p50:            float = 0.0
_p75:            float = 0.0
_phase:          int   = 0     # 0/1/2/3  (3 = at/beyond target)

_active:          bool  = False
_stopped_out:     bool  = False
_expiry_afternoon: bool  = False   # per-tick multiple SL active on expiry day

# Expiry afternoon per-tick step SL — locks in at price multiples (intrabar spike protection)
_EXPIRY_STEPS = [
    (5.0, 3.5),   # 5× entry → SL at 3.5×
    (3.0, 2.0),   # 3× entry → SL at 2.0×
    (2.0, 1.5),   # 2× entry → SL at 1.5×
]


def _advance_phase(ltp: float) -> None:
    """
    Advance SL phase based on progress milestones.
    Called ONLY on 5-min bar close — not every tick — to avoid noise shakeouts.

    Phases (progress = (ltp - entry) / range):
      0: 0–33%   → floor = initial_sl       (no movement)
      1: ≥33%    → floor = entry             (breakeven)
      2: ≥66%    → floor = entry + 25%×range
      3: ≥100%   → floor = entry + 50%×range (target extends separately)
    """
    global _phase, _sl_floor

    if not _entry_price or not _target:
        return

    rng      = _target - _entry_price
    progress = (ltp - _entry_price) / rng if rng > 0 else 0.0

    if _phase < 3 and progress >= 1.0:
        _phase    = 3
        _sl_floor = round(_entry_price + 0.50 * rng, 1)
        log.info("Trail SL phase→3 (100%% bar close): SL floor → %.1f", _sl_floor)

    elif _phase < 2 and progress >= 0.66:
        _phase    = 2
        _sl_floor = round(_entry_price + 0.25 * rng, 1)
        log.info("Trail SL phase→2 (66%% bar close): SL floor → %.1f", _sl_floor)

    elif _phase < 1 and progress >= 0.33:
        _phase    = 1
        _sl_floor = _entry_price
        log.info("Trail SL phase→1 (33%% bar close): SL floor → %.1f (breakeven)", _sl_floor)


# ── public API ────────────────────────────────────────────────────────────────

def set_entry(
    price: float,
    side: str,
    option_key: str       = "",
    trail_pct: float | None = None,   # noqa: kept for API compat, no longer used
    target: float         = 0.0,
    initial_sl: float     = 0.0,
    expiry_afternoon: bool = False,
) -> None:
    """Milestone-based trailing SL. On expiry_afternoon, also applies per-tick multiple SL."""
    global _entry_price, _initial_sl, _initial_risk, _half_risk, _target, _side, _option_key
    global _peak, _trail_sl, _trail_pct, _sl_floor
    global _p50, _p75, _phase, _active, _stopped_out, _expiry_afternoon

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

    _initial_risk = round(price - _initial_sl, 1)   # 1R distance
    _half_risk    = round(_initial_risk / 2, 1)      # half-R — post-phase1 bar trail
    _sl_floor     = _initial_sl
    _trail_sl     = _initial_sl

    rng  = (_target - _entry_price) if _target > _entry_price else 0.0
    _p50 = round(_entry_price + 0.25 * rng, 1) if rng else 0.0   # phase-2 floor
    _p75 = round(_entry_price + 0.50 * rng, 1) if rng else 0.0   # phase-3 floor

    log.info(
        "Trail SL: %s %s @ %.1f  SL=%.1f  tgt=%.1f  "
        "floors: BE=%.1f  p2=%.1f  p3=%.1f  p4=%.1f",
        side, option_key, price, _trail_sl, _target,
        _entry_price, _p50, _p75,
        round(_entry_price + 0.75 * rng, 1) if rng else 0.0,
    )


def on_bar_close(ltp: float) -> None:
    """
    Call at every 5-min bar close.
    Phase 0-2: advance SL floor when milestone reached.
    Phase 3+ (at/beyond target): trail SL = bar_close - 1R, so SL follows price up.
    """
    global _trail_sl, _sl_floor
    if not _active or not ltp:
        return
    _advance_phase(ltp)
    if _phase >= 3 and _initial_risk > 0:
        # Post-target: trail 1R below bar close
        bar_trail = round(ltp - _initial_risk, 1)
        if bar_trail > _sl_floor:
            _sl_floor = bar_trail
            log.info("Post-target trail: bar_close=%.1f  SL → %.1f", ltp, _sl_floor)
    elif _phase >= 1 and _half_risk > 0:
        # Post-breakeven: trail half-R below bar close — captures small moves
        bar_trail = round(ltp - _half_risk, 1)
        if bar_trail > _sl_floor:
            _sl_floor = bar_trail
            log.info("Post-BE trail: bar_close=%.1f  SL → %.1f", ltp, _sl_floor)
    _trail_sl = max(_sl_floor, _trail_sl)


def update(instrument_key: str, ltp: float) -> bool:
    """
    Called every tick. Checks SL hit against current floor.
    Phase advancement happens only in on_bar_close() — not here.
    Returns True when SL triggered for first time.
    """
    global _peak, _trail_sl, _stopped_out, _sl_floor

    if not _active or not ltp or not _option_key:
        return False
    if instrument_key != _option_key:
        return False

    if ltp > _peak:
        _peak = ltp

    # Expiry afternoon: per-tick multiple SL — catches intrabar spikes that retrace
    # Works alongside bar-close phase trail; whichever floor is higher wins
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

    _trail_sl = _sl_floor   # floor set by on_bar_close() + expiry per-tick above

    if not _stopped_out and ltp <= _trail_sl:
        _stopped_out = True
        log.warning(
            "SL triggered phase=%d: ltp=%.1f <= sl=%.1f  entry=%.1f  peak=%.1f",
            _phase, ltp, _trail_sl, _entry_price, _peak,
        )
        return True
    return False


def shift_target(new_target: float) -> None:
    """Extend target forward (called when price blows past original target in trending market)."""
    global _target, _p50, _p75
    if not _active or new_target <= _target:
        return
    _target = new_target
    rng  = (_target - _entry_price) if _target > _entry_price else 0.0
    _p50 = round(_entry_price + 0.50 * rng, 1) if rng else _p50
    _p75 = round(_entry_price + 0.75 * rng, 1) if rng else _p75
    log.info("Target shifted → %.1f  new p50=%.1f  p75=%.1f", _target, _p50, _p75)


def exit_trade() -> None:
    global _active, _stopped_out, _phase, _expiry_afternoon
    _active           = False
    _stopped_out      = False
    _phase            = 0
    _expiry_afternoon = False
    log.info("Trail SL: exited")


def is_active() -> bool:
    return _active


def get_status() -> dict:
    rng      = (_target - _entry_price) if (_target and _entry_price) else 0.0
    progress = round((_peak - _entry_price) / rng * 100, 1) if rng > 0 else 0.0
    return {
        "active":           _active,
        "stopped_out":      _stopped_out,
        "expiry_afternoon": _expiry_afternoon,
        "entry":            _entry_price,
        "target":           _target,
        "side":             _side,
        "peak":             _peak,
        "trail_sl":         _trail_sl,
        "sl_floor":         _sl_floor,
        "trail_pct":        _trail_pct,
        "phase":            _phase,
        "progress_pct":     progress,
        "pnl_pts":          round(_peak - _entry_price, 1) if _active else 0.0,
        "in_profit":        _active and _peak > _entry_price,
        "multiple":         round(_peak / _entry_price, 2) if (_active and _entry_price) else 1.0,
    }
