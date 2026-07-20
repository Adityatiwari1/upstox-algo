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

Events:
  break_up/down     on bar CLOSE only, against the strict 10/10 level — a
                    break is only meaningful against a fully-proven level,
                    and needs the whole bar to rule out a wick-only poke
                    (the wick filter needs the complete bar to check).
  bounce_up/down    TWO-STAGE signal-bar + confirmation pattern (2026-07-20,
                    user spec — replaces the old single-bar reject test):
                      1. SIGNAL bar (closed, checked in update()): its low
                         tags support within TOL_PTS (or pierces and comes
                         back) AND its close lands within TOL_PTS of
                         support, above it — e.g. support 24150, bar high
                         24168, low touches, closes 24152-24154. Arms with
                         that bar's HIGH as the trigger.
                      2. CONFIRMATION: only during the SINGLE next 5-min
                         bar, checked every TICK against the live running
                         high — the moment price trades up through
                         signal_high + CONFIRM_PTS (e.g. 24168+5=24173),
                         fire immediately, intrabar, no bar-close wait.
                         Unconfirmed after that one candle → signal expires.
                    Mirror at resistance for PE (signal bar's LOW is the
                    trigger; confirmation watches the live running low
                    breaking below trigger - CONFIRM_PTS).
                    Uses the FAST level (see tiers below) as the support/
                    resistance a bar can signal off of.
  retest_up/down    within 6 bars of a break, price revisits the broken
                    level and closes back in the break direction.

Three confirmation tiers (2026-07-20, user):
  strict pivots  (LEFT=10/RIGHT=10, ~50min lag) — powers break_up/down. A
    level has to be fully proven before its violation means anything.
  fast pivots    (LEFT=10/RIGHT=3,  ~15min lag) — powers bounce_up/down. A
    bounce is self-validating (the reject move IS the evidence a low/high
    held) so it doesn't need the full 50min wait a break does.
  OI walls       (zero lag)  — call_wall/put_wall (option_chain_service):
    the strike carrying the most CE/PE OI, right now, no forward bars
    needed since it's not a price-shape claim, it's a live positioning
    fact. Joined into BOTH pools at read time, so it's available the
    instant the chain has OI. Tradeoff: it can move under you as OI is
    written/unwound intraday — no repaint-lock like the pivot tiers have.
Honest limit: neither pivot tier is instant — a "confirmed" extreme by
definition needs the market to have already moved past it. A violent V
(session low to +90pt in under 20min, as on 2026-07-20) can outrun even the
fast tier; that day's specific reversal likely still wasn't catchable by
bounce, only by retest if price returned to the level later (it didn't,
that day) — unless an OI wall happened to sit right at the extreme. Lux is
an S/R reaction system, not a first-move momentum system — range_break/
sustained/orb exist for "no known level yet" moves.

check_entry(option) maps events to entry types — lux_break / lux_bounce /
lux_retest — one trade per (level, side) per day.
"""
from __future__ import annotations

import logging
import time

log = logging.getLogger(__name__)

LEFT         = 10
RIGHT        = 10      # strict tier — break_up/down
RIGHT_FAST   = 3       # fast tier — bounce signal bars (~15min vs ~50min lag)
TOL_PTS      = 8.0     # touch/near-close tolerance for signal bars and retest
CONFIRM_PTS  = 5.0     # clearance beyond the signal bar's high/low to confirm
RETEST_BARS  = 6       # bars after a break in which a retest can confirm
MERGE_PTS    = 5.0     # pivots closer than this collapse into one level
N_BARS       = 450     # 5 trading days (375) + today
_BARS_NEEDED = LEFT + RIGHT + 2

NIFTY = "NSE_INDEX|Nifty 50"

_slot:        int = 0
_res:         float = 0.0   # active strict: nearest level above last close
_sup:         float = 0.0   # active strict: nearest level below last close
_res_fast:    float = 0.0   # active fast: nearest level above last close
_sup_fast:    float = 0.0   # active fast: nearest level below last close
_levels:      list[float] = []   # all merged STRICT pivot levels in the window
_fast_levels: list[float] = []   # all merged FAST pivot levels in the window
_last_close:  float = 0.0
_event:       tuple[int, str, float] | None = None   # (slot, event, level)
_break_watch: tuple[int, str, float] | None = None   # (slot, "up"|"down", level) for retest
_sig_sup:     tuple[int, float, float] | None = None  # (armed_slot, support, trigger_high)
_sig_res:     tuple[int, float, float] | None = None  # (armed_slot, resistance, trigger_low)
_traded:      dict[str, float] = {}   # "CE"/"PE" → level already traded today
_traded_date: str = ""
_live_fired:  tuple[int, str, float] | None = None   # bounce already fired this forming bar

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


def evaluate_break(bars: list, res: float, sup: float) -> tuple[str, float] | None:
    """Break event on the last CLOSED bar, or None. Pure function. Needs the
    whole bar (wick filter) — a break is only meaningful against the strict,
    fully-proven level. No volume gate (user 2026-07-17): flow_score + the
    score gate + divergence block already provide live participation
    evidence in the entry pipeline — the pine osc would duplicate them,
    uncalibrated. The osc is still computed and LOGGED per event so the
    weekly audit can show whether it would ever have added value."""
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
    return None


def signal_bar_support(l: float, c: float, support: float) -> bool:
    """Does this CLOSED bar qualify as a support signal bar? Low tags
    support (or pierces and recovers) and the close lands near support,
    above it — e.g. support 24150: low touches ≤24158, close within
    24150-24158. Pure function."""
    return bool(support) and l <= support + TOL_PTS and 0 <= c - support <= TOL_PTS


def signal_bar_resistance(h: float, c: float, resistance: float) -> bool:
    """Mirror of signal_bar_support at resistance: high tags resistance,
    close lands near it from below."""
    return bool(resistance) and h >= resistance - TOL_PTS and 0 <= resistance - c <= TOL_PTS


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


def oi_wall_levels() -> list[float]:
    """Call-wall / put-wall strikes — the strike with the most CE/PE OI in
    the live chain. Zero confirmation lag (2026-07-20, user): unlike a
    fractal pivot, a wall needs no forward bars to prove it — it IS the
    level right now, by definition, on every read. Tradeoff: it can move
    under you as OI is written/unwound through the day (no repaint-lock)."""
    from uptox.services import option_chain_service
    out: list[float] = []
    cw = option_chain_service.get_call_wall()
    if cw:
        out.append(float(cw[0]))
    pw = option_chain_service.get_put_wall()
    if pw:
        out.append(float(pw[0]))
    return out


def update() -> None:
    """Called every dashboard refresh; does work only when a 5-min bar closes."""
    global _slot, _res, _sup, _res_fast, _sup_fast, _levels, _fast_levels
    global _last_close, _event, _break_watch, _sig_sup, _sig_res
    slot = int(time.time() // 300)
    if slot == _slot:
        return
    _slot = slot

    from uptox.services import sma_service
    from uptox.services.sma_service import TimeFrame
    bars = sma_service.get_last_candles(NIFTY, TimeFrame.M5, n=N_BARS)
    if len(bars) < _BARS_NEEDED:
        return

    ph, pl = pivots(bars, LEFT, RIGHT)
    _levels = merge_levels(ph, pl)
    ph_fast, pl_fast = pivots(bars, LEFT, RIGHT_FAST)
    _fast_levels = merge_levels(ph_fast, pl_fast)
    _last_close = bars[-1][3]
    walls = oi_wall_levels()   # zero-lag, joined into both tiers fresh each call
    # Active pair anchored at the PREVIOUS close: events detect the last bar
    # CROSSING a level, so the level must be picked from where price stood
    # before the bar, not after it
    new_res, new_sup = _nearest(_levels + walls, bars[-2][3])
    new_res_fast, new_sup_fast = _nearest(_fast_levels + walls, bars[-2][3])
    if new_res != _res or new_sup != _sup:
        log.info("[lux-sr] strict levels: res=%.1f sup=%.1f (%d pivots + %d walls)",
                 new_res, new_sup, len(_levels), len(walls))
    if new_res_fast != _res_fast or new_sup_fast != _sup_fast:
        log.info("[lux-sr] fast levels: res=%.1f sup=%.1f (%d pivots + %d walls)",
                 new_res_fast, new_sup_fast, len(_fast_levels), len(walls))
    _res, _sup = new_res, new_sup
    _res_fast, _sup_fast = new_res_fast, new_sup_fast

    vol = _chain_bar_volume()
    if vol is not None:
        _bar_vols.append(vol)
        del _bar_vols[:-12]
    osc = _osc()

    ev = evaluate_break(bars, _res, _sup)

    # Bounce signal bars: an unconfirmed signal only lives through the ONE
    # bar right after it forms — if that bar just closed without the live
    # confirm firing, the setup is stale, expire it before arming a new one.
    o, h, l, c = bars[-1]
    if _sig_sup and slot > _sig_sup[0] + 1:
        _sig_sup = None
    if _sig_res and slot > _sig_res[0] + 1:
        _sig_res = None
    if signal_bar_support(l, c, _sup_fast):
        _sig_sup = (slot, _sup_fast, h)
        log.info("[lux-sr] signal bar (support): sup=%.1f bar_high=%.1f "
                 "— next candle must clear %.1f to confirm CE",
                 _sup_fast, h, h + CONFIRM_PTS)
    if signal_bar_resistance(h, c, _res_fast):
        _sig_res = (slot, _res_fast, l)
        log.info("[lux-sr] signal bar (resistance): res=%.1f bar_low=%.1f "
                 "— next candle must clear %.1f to confirm PE",
                 _res_fast, l, l - CONFIRM_PTS)

    # Retest: a pullback to a recently broken level that closes back in the
    # break's direction. Checked before overwriting the break watch.
    if ev is None and _break_watch:
        b_slot, b_dir, b_lvl = _break_watch
        if slot - b_slot > RETEST_BARS:
            _break_watch = None
        else:
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


def check_live_bounce() -> None:
    """Called every tick (not slot-gated): confirms an armed bounce signal
    the moment live price clears the signal bar's high/low by CONFIRM_PTS —
    2026-07-20 user spec: support 24150, signal bar high 24168, close
    24152-24154; next candle must trade up through ~24173 to confirm CE.
    Uses the forming bar's running high/low (updated on every tick already)
    so the check fires the instant the level is cleared, not at bar close.
    Only valid during the SINGLE bar right after the signal armed — see the
    expiry in update()."""
    global _event, _live_fired
    if not _sig_sup and not _sig_res:
        return
    from uptox.services import sma_service
    from uptox.services.sma_service import TimeFrame
    forming = sma_service.get_forming_bar(NIFTY, TimeFrame.M5)
    if not forming:
        return
    cur_slot = int(time.time() // 300)
    if _live_fired and _live_fired[0] == cur_slot:
        return
    _, h, l, _ = forming

    if _sig_sup and cur_slot == _sig_sup[0] + 1:
        support, trigger_high = _sig_sup[1], _sig_sup[2]
        if h >= trigger_high + CONFIRM_PTS:
            _live_fired = (cur_slot, "bounce_up", support)
            _event = (cur_slot, "bounce_up", support)
            log.info("[lux-sr] LIVE bounce_up confirmed: cleared %.1f (sup=%.1f)",
                     trigger_high + CONFIRM_PTS, support)
            return

    if _sig_res and cur_slot == _sig_res[0] + 1:
        resistance, trigger_low = _sig_res[1], _sig_res[2]
        if l <= trigger_low - CONFIRM_PTS:
            _live_fired = (cur_slot, "bounce_down", resistance)
            _event = (cur_slot, "bounce_down", resistance)
            log.info("[lux-sr] LIVE bounce_down confirmed: cleared %.1f (res=%.1f)",
                     trigger_low - CONFIRM_PTS, resistance)


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
    One trade per (level, side) per day. Gated on the REAL current slot, not
    the stored _slot (which only tracks the last CLOSED bar) — a live
    intrabar bounce event is stamped with the forming bar's slot, which
    equals int(time.time()//300) at fire time, same as it would for a
    bar-close event evaluated the instant its bar closes."""
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


def get_block_levels() -> list[tuple[str, float]]:
    """Nearest 2 labeled levels per side of the last close, for _near_sr
    blocking — pivots plus the live OI walls, so the block message names
    the real obstacle ("Resistance: Call Wall (Npt above)")."""
    from uptox.services import option_chain_service
    pool: list[tuple[str, float]] = [("Swing High/Low", v) for v in _levels]
    cw = option_chain_service.get_call_wall()
    if cw:
        pool.append(("Call Wall", float(cw[0])))
    pw = option_chain_service.get_put_wall()
    if pw:
        pool.append(("Put Wall", float(pw[0])))
    above = sorted((v, lbl) for lbl, v in pool if v > _last_close)[:2]
    below = sorted(((v, lbl) for lbl, v in pool if v < _last_close), reverse=True)[:2]
    return [(lbl, v) for v, lbl in above] + [(lbl, v) for v, lbl in below]
