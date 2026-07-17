"""
LuxAlgo-style Support/Resistance with breaks — calculation port.

Source idea: "Support and Resistance Levels with Breaks [LuxAlgo]"
(CC BY-NC-SA 4.0). Only the S/R + volume-oscillator calculation is ported —
none of the original's plotting/alert/signal code.

Levels — fractal pivots on closed 5-min bars, LEFT=10 / RIGHT=10 wings:
a bar whose high strictly tops the 10 bars on each side is a resistance
pivot, confirmed only after its 10 right-wing bars close (levels appear
~50 min after the extreme prints — strong, non-repainting). Window = the
last 5 trading days + today (user 2026-07-17, ~450 bars): ALL confirmed
pivots in the window form the level set (near-duplicates within 5pt merge,
newest kept), and the ACTIVE pair is the nearest level above price
(resistance) and nearest below (support) — so a level broken upward
automatically flips to support on the next selection, and multi-day levels
stay tradeable, not just the newest swing (upgrade over pine's fixnan,
which only ever watches the single most recent pivot). No overnight state:
everything is recomputed from the candle store at every bar close, so each
morning simply reads the freshly primed candles.

Volume — DIAGNOSTIC ONLY (user decision 2026-07-17): the pine volume gate
is not enforced, because the entry pipeline already demands live
participation evidence (flow_score alignment, the score gate, the
SMA-vs-Flow divergence block). The oscillator is still computed — per-5min
sum of |ΔOI| across subscribed strikes (per-key diffs, roll-proof),
osc = 100*(ema5-ema10)/ema10 — and logged with each event, so the weekly
audit can test whether gating on it would ever have added value.

Events — evaluated once per closed bar, actionable until the next close:
  break_up     close crosses above resistance, body-driven
               (pine wick test: NOT open-low > close-open)
  break_down   mirror through support
  bounce_up    bar's low tags support (±TOL) and closes green above it
  bounce_down  mirror at resistance
  retest_up    within 6 bars of a break_up, price pulls back to the broken
               level (±TOL) and closes green — break-then-retest long
  retest_down  mirror

check_entry(option) maps events to entry types — lux_break / lux_bounce /
lux_retest — one trade per (level, side) per day.
"""
from __future__ import annotations

import logging
import time

log = logging.getLogger(__name__)

LEFT         = 10
RIGHT        = 10
TOL_PTS      = 8.0     # level touch tolerance for bounce/retest
RETEST_BARS  = 6       # bars after a break in which a retest can confirm
MERGE_PTS    = 5.0     # pivots closer than this collapse into one level
N_BARS       = 450     # 5 trading days (375) + today
_BARS_NEEDED = LEFT + RIGHT + 2

_slot:        int = 0
_res:         float = 0.0   # active: nearest level above last close
_sup:         float = 0.0   # active: nearest level below last close
_levels:      list[float] = []   # all merged pivot levels in the window
_last_close:  float = 0.0
_event:       tuple[int, str, float] | None = None   # (slot, event, level)
_break_watch: tuple[int, str, float] | None = None   # (slot, "up"|"down", level) for retest
_traded:      dict[str, float] = {}   # "CE"/"PE" → level already traded today
_traded_date: str = ""

_prev_oi:   dict[str, float] = {}
_bar_vols:  list[float] = []   # last 12 per-bar chain |ΔOI| sums


def pivots(bars: list, left: int = LEFT, right: int = RIGHT) -> tuple[list[float], list[float]]:
    """Confirmed fractal pivot highs/lows (oldest→newest). Pure function.
    bars: (o, h, l, c) tuples; only indices with full wings both sides count."""
    highs = [b[1] for b in bars]
    lows  = [b[2] for b in bars]
    ph: list[float] = []
    pl: list[float] = []
    for i in range(left, len(bars) - right):
        wings_h = highs[i - left:i] + highs[i + 1:i + right + 1]
        wings_l = lows[i - left:i] + lows[i + 1:i + right + 1]
        if all(highs[i] > x for x in wings_h):
            ph.append(highs[i])
        if all(lows[i] < x for x in wings_l):
            pl.append(lows[i])
    return ph, pl


def evaluate(bars: list, res: float, sup: float) -> tuple[str, float] | None:
    """Event on the last closed bar, or None. Pure function.
    No volume gate (user decision 2026-07-17): flow_score + the score gate
    + divergence block already provide live participation evidence in the
    entry pipeline — the pine osc would duplicate them, uncalibrated. The
    osc is still computed and LOGGED per event so the weekly audit can show
    whether it would ever have added value."""
    if len(bars) < 2:
        return None
    o, h, l, c = bars[-1]
    prev_close = bars[-2][3]

    if res and prev_close <= res < c:
        if not (o - l > c - o):   # body-driven, not a wick poke
            return "break_up", res
    if sup and prev_close >= sup > c:
        if not (o - c < h - o):
            return "break_down", sup
    if sup and l <= sup + TOL_PTS and c > sup and c > o and prev_close > sup:
        return "bounce_up", sup
    if res and h >= res - TOL_PTS and c < res and c < o and prev_close < res:
        return "bounce_down", res
    return None


def _chain_bar_volume() -> float | None:
    """Chain positioning activity since the previous bar: sum of per-key
    |ΔOI| across subscribed strikes. None = no data yet."""
    global _prev_oi
    from uptox.services import feed_store, option_chain_service
    now: dict[str, float] = {}
    for k in option_chain_service.get_all_subscribed_keys():
        t = feed_store.get(k)
        if t and t.oi:
            now[k] = t.oi
    if not now:
        return None
    vol = sum(abs(v - _prev_oi[k]) for k, v in now.items() if k in _prev_oi)
    had_prev = bool(_prev_oi)
    _prev_oi = now
    return vol if had_prev else None


def _osc() -> float | None:
    if len(_bar_vols) < 10:
        return None
    def ema(vals: list[float], n: int) -> float:
        a = 2.0 / (n + 1)
        e = vals[0]
        for v in vals[1:]:
            e = a * v + (1 - a) * e
        return e
    e5, e10 = ema(_bar_vols[-10:], 5), ema(_bar_vols[-10:], 10)
    return 100.0 * (e5 - e10) / e10 if e10 else None


def merge_levels(ph: list[float], pl: list[float]) -> list[float]:
    """Collapse all pivots (both sides) into one deduped level list, newest
    wins within MERGE_PTS. Pure function."""
    out: list[float] = []
    for v in reversed(ph + pl):          # newest last in input → scan newest first
        if all(abs(v - kept) > MERGE_PTS for kept in out):
            out.append(v)
    return sorted(out)


def _nearest(levels: list[float], price: float) -> tuple[float, float]:
    """(nearest level above price, nearest below). 0.0 = none on that side."""
    above = [v for v in levels if v > price]
    below = [v for v in levels if v < price]
    return (min(above) if above else 0.0), (max(below) if below else 0.0)


def update() -> None:
    """Called every dashboard refresh; does work only when a 5-min bar closes."""
    global _slot, _res, _sup, _levels, _last_close, _event, _break_watch
    slot = int(time.time() // 300)
    if slot == _slot:
        return
    _slot = slot

    from uptox.services import sma_service
    from uptox.services.sma_service import TimeFrame
    bars = sma_service.get_last_candles("NSE_INDEX|Nifty 50", TimeFrame.M5, n=N_BARS)
    if len(bars) < _BARS_NEEDED:
        return

    ph, pl = pivots(bars)
    _levels = merge_levels(ph, pl)
    _last_close = bars[-1][3]
    # Active pair anchored at the PREVIOUS close: evaluate() detects the last
    # bar CROSSING a level, so the level must be picked from where price
    # stood before the bar, not after it
    new_res, new_sup = _nearest(_levels, bars[-2][3])
    if new_res != _res or new_sup != _sup:
        log.info("[lux-sr] levels: res=%.1f sup=%.1f (%d in window)",
                 new_res, new_sup, len(_levels))
    _res, _sup = new_res, new_sup

    vol = _chain_bar_volume()
    if vol is not None:
        _bar_vols.append(vol)
        del _bar_vols[:-12]
    osc = _osc()

    ev = evaluate(bars, _res, _sup)

    # Retest: a pullback to a recently broken level that closes back in the
    # break's direction. Checked before overwriting the break watch.
    if ev is None and _break_watch:
        b_slot, b_dir, b_lvl = _break_watch
        if slot - b_slot > RETEST_BARS:
            _break_watch = None
        else:
            o, h, l, c = bars[-1]
            if b_dir == "up" and l <= b_lvl + TOL_PTS and c > b_lvl and c > o:
                ev = ("retest_up", b_lvl)
            elif b_dir == "down" and h >= b_lvl - TOL_PTS and c < b_lvl and c < o:
                ev = ("retest_down", b_lvl)

    if ev:
        name, lvl = ev
        if name == "break_up":
            _break_watch = (slot, "up", lvl)
        elif name == "break_down":
            _break_watch = (slot, "down", lvl)
        _event = (slot, name, lvl)
        log.info("[lux-sr] %s @ %.1f (osc=%s)", name, lvl,
                 f"{osc:.0f}" if osc is not None else "warmup")


_ENTRY_MAP = {
    ("CE", "break_up"):    "lux_break",
    ("CE", "bounce_up"):   "lux_bounce",
    ("CE", "retest_up"):   "lux_retest",
    ("PE", "break_down"):  "lux_break",
    ("PE", "bounce_down"): "lux_bounce",
    ("PE", "retest_down"): "lux_retest",
}


def check_entry(option: str) -> str | None:
    """Entry type if the current bar's event matches the trade direction.
    One trade per (level, side) per day."""
    global _traded, _traded_date
    if _event is None or _event[0] != _slot:
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


def get_block_levels() -> list[tuple[str, float]]:
    """Nearest 2 levels per side of the last close, for _near_sr blocking."""
    above = sorted(v for v in _levels if v > _last_close)[:2]
    below = sorted((v for v in _levels if v < _last_close), reverse=True)[:2]
    return ([("Swing High", v) for v in above]
            + [("Swing Low", v) for v in below])
