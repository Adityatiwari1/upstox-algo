"""
Consolidation Range breakout/breakdown — ported from the user's Pine Script
"Consolidation Range Detector" indicator.

Complements range_break (dashboard.py): that one watches a 3-bar (15min)
window with a 25pt tightness cap and a 7pt departure — fast, but only
holds onto a *short, narrow* coil. This one watches a 7-bar (35min)
window with a %-of-price tightness cap and requires the range to stay
tight for at least 10 of those bars before the box counts as "confirmed"
(user-tuned 2026-07-23, from the Pine script's own 10-bar/0.5%/8-bar
defaults) — built for a *slower, wider* consolidation that a 3-bar window
can't stay inside long enough to confirm (2026-07-23: a ~35min/~35pt chop
the existing range_break never armed against, then the eventual lux_break
entry only fired once the level's 50min pivot-confirmation lag cleared,
capturing far less of the move than a live consolidation-box break would
have).

Departure confirmation deviates from the original Pine script on purpose:
the Pine version fires the instant price exits the tight range at all
(any close outside the box = "Breakout"/"Breakdown"). That's noisy at
index-point scale — a single wick past the box edge isn't real
conviction. Same discipline as lux_sr's CONFIRM_PTS: price must clear the
box edge by DEPARTURE_PTS (checked every tick against the live running
bar, not just at bar close) before the break/breakdown event fires.

Events:
  consol_break      live NIFTY clears box_high + DEPARTURE_PTS after a
                    CONFIRMED box (>= MIN_BARS held tight) is left.
  consol_breakdown  mirror, clears box_low - DEPARTURE_PTS.

check_entry(option) maps events to entry types — consol_break /
consol_breakdown — one trade per box per day, same one-shot discipline as
lux_sr.check_entry.
"""
from __future__ import annotations

import logging
import time

log = logging.getLogger(__name__)

LOOKBACK        = 7      # bars used to measure box tightness (user-tuned 2026-07-23)
RANGE_THRESH_PCT = 0.2   # max (high-low)/low % over the lookback to count as tight
MIN_BARS        = 10     # box must hold tight this many bars before it's "confirmed"
DEPARTURE_PTS   = 15.0   # clearance beyond the box edge to confirm a real break

NIFTY = "NSE_INDEX|Nifty 50"

_slot:            int = 0
_in_consolidation: bool = False
_bar_count:       int = 0
_box_high:        float = 0.0
_box_low:         float = 0.0
_confirmed:       bool = False
_watch:           tuple[int, float, float] | None = None   # (armed_slot, box_high, box_low)
_event:           tuple[int, str, float] | None = None      # (slot, event, level)
_traded:          dict[str, float] = {}
_traded_date:     str = ""


def box_range(bars: list, lookback: int = LOOKBACK) -> tuple[float, float] | None:
    """Highest high / lowest low over the last `lookback` closed bars, or
    None if there isn't enough history yet. Pure function.
    bars: (o, h, l, c) tuples, oldest first."""
    if len(bars) < lookback:
        return None
    window = bars[-lookback:]
    hh = max(b[1] for b in window)
    ll = min(b[2] for b in window)
    return hh, ll


def is_tight(hh: float, ll: float, thresh_pct: float = RANGE_THRESH_PCT) -> bool:
    """(high-low)/low as a %, within thresh_pct = tight enough to count as
    consolidation. Pure function."""
    if ll <= 0:
        return False
    return (hh - ll) / ll * 100 <= thresh_pct


def update() -> None:
    """Called every dashboard refresh; does work only when a 5-min bar
    closes — same cadence as lux_sr.update()."""
    global _slot, _in_consolidation, _bar_count, _box_high, _box_low
    global _confirmed, _watch
    slot = int(time.time() // 300)
    if slot == _slot:
        return
    _slot = slot

    from uptox.services import sma_service
    from uptox.services.sma_service import TimeFrame
    bars = sma_service.get_last_candles(NIFTY, TimeFrame.M5, n=LOOKBACK)
    box = box_range(bars, LOOKBACK)
    if box is None:
        return
    hh, ll = box
    tight = is_tight(hh, ll)

    if tight:
        if not _in_consolidation:
            _box_high, _box_low = hh, ll
            _bar_count = LOOKBACK
            _in_consolidation = True
        else:
            _box_high = max(_box_high, hh)
            _box_low  = min(_box_low, ll)
            _bar_count += 1
        _confirmed = _bar_count >= MIN_BARS
        _watch = None   # still inside the range -- nothing to watch yet
    else:
        if _in_consolidation and _confirmed:
            # just left a box that was genuinely confirmed -- arm a
            # departure watch, don't fire until check_live clears it
            _watch = (slot, _box_high, _box_low)
            log.info("[consol] left confirmed box [%.1f-%.1f] -- watching for "
                      "%.1fpt departure", _box_low, _box_high, DEPARTURE_PTS)
        _in_consolidation = False
        _bar_count = 0
        _confirmed = False


def check_live(ltp: float) -> None:
    """Called every tick: fires the instant live price clears the armed
    box edge by DEPARTURE_PTS -- intrabar, not waiting for the next bar
    close, same discipline as lux_sr.check_live_bounce."""
    global _event, _watch
    if not _watch or not ltp:
        return
    _, box_high, box_low = _watch
    if ltp >= box_high + DEPARTURE_PTS:
        cur_slot = int(time.time() // 300)
        _event = (cur_slot, "consol_break", box_high)
        _watch = None
        log.info("[consol] BREAK confirmed: cleared %.1f (box_high=%.1f)",
                  box_high + DEPARTURE_PTS, box_high)
    elif ltp <= box_low - DEPARTURE_PTS:
        cur_slot = int(time.time() // 300)
        _event = (cur_slot, "consol_breakdown", box_low)
        _watch = None
        log.info("[consol] BREAKDOWN confirmed: cleared %.1f (box_low=%.1f)",
                  box_low - DEPARTURE_PTS, box_low)


_ENTRY_MAP = {
    ("CE", "consol_break"):     "consol_break",
    ("PE", "consol_breakdown"): "consol_breakdown",
}


def check_entry(option: str) -> str | None:
    """Entry type if the current bar/tick's event matches the trade
    direction. One trade per (level, side) per day -- same shape as
    lux_sr.check_entry."""
    global _traded, _traded_date
    if _event is None or _event[0] != int(time.time() // 300):
        return None
    et = _ENTRY_MAP.get((option, _event[1]))
    if et is None:
        return None
    from datetime import date
    today = date.today().isoformat()
    if _traded_date != today:
        _traded, _traded_date = {}, today
    if _traded.get(option) == _event[2]:
        return None
    _traded[option] = _event[2]
    return et
