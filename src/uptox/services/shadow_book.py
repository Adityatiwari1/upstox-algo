"""
Shadow book — live tracker for entries focus mode benched.

When focus mode blocks an indicator-driven entry (sustained/momentum/retest),
this opens a SHADOW position at the real ATM option premium and manages it on
live ticks, so the Jul-21 review can compare focus mode against what the old
all-entries system would have traded — at real prices, not the 0.5-delta
estimate in backtest.focus_compare.

Never places orders, never touches trade_book/capital/telegram. One shadow at
a time, none while a real trade is open (old mode was single-position and
would have been holding that same trade), 5-min cooldown after a shadow close.

Exit model (shadow-v1, simpler than the live stack by design):
    hard stop      -10 option pts (MAE stop dominates real losses)
    profit lock    peak >= 6pt -> floor entry + 50% of peak; >= 15pt -> 60%
    session close  15:20 IST (checked on every tick, any instrument)

Events append to logs/shadow_trades.jsonl: one "open" row, one "close" row
per shadow. An open without a close means the app restarted mid-shadow.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

log  = logging.getLogger(__name__)
_IST = ZoneInfo("Asia/Kolkata")

_FILE = Path("logs/shadow_trades.jsonl")

_STOP_PTS       = 10.0
_LOCK_ARM_PTS   = 6.0
_LOCK_KEEP      = 0.50
_LOCK_BIG_PTS   = 15.0
_LOCK_BIG_KEEP  = 0.60
_COOLDOWN_S     = 300.0
_SESSION_CLOSE  = (15, 20)

_shadow: dict | None = None
_cooldown_until: float = 0.0


def _append(row: dict) -> None:
    try:
        with _FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")
    except OSError as e:
        log.warning("shadow_book: could not write %s: %s", _FILE, e)


def try_open(entry_type: str, option: str, spot_ltp: float, score: int) -> None:
    """Called from the dashboard's focus-mode block. All guards internal."""
    global _shadow
    if _shadow or time.time() < _cooldown_until:
        return
    from uptox.services import feed_store, option_chain_service, trade_book
    if trade_book.get_open_trade():
        return
    atm  = round(spot_ltp / 50) * 50
    key  = option_chain_service.get_key(atm, option)
    tick = feed_store.get(key) if key else None
    if not tick or not tick.ltp:
        return
    now = datetime.now(_IST)
    _shadow = {
        "id":          f"sh-{now.strftime('%H%M%S')}",
        "date":        now.strftime("%Y-%m-%d"),
        "entry_type":  entry_type,
        "option":      option,
        "strike":      atm,
        "option_key":  key,
        "entry_price": round(tick.ltp, 1),
        "spot_entry":  round(spot_ltp, 1),
        "score":       score,
        "entry_ts":    time.time(),
        "peak":        tick.ltp,
        "last_price":  tick.ltp,
        "last_tick_ts": time.time(),
    }
    _append({"event": "open", "ts": now.isoformat(), **{
        k: _shadow[k] for k in
        ("id", "date", "entry_type", "option", "strike", "option_key",
         "entry_price", "spot_entry", "score")
    }})
    log.info("[shadow] OPEN %s %s %d @ %.1f (score %+d, focus benched)",
             entry_type, option, atm, tick.ltp, score)


def on_tick(instrument_key: str, ltp: float) -> None:
    """Called for every market tick (any instrument)."""
    if _shadow is None or not ltp:
        return
    now = datetime.now(_IST)
    if (now.hour, now.minute) >= _SESSION_CLOSE:
        _close(_shadow["last_price"], "session_close",
               stale=(time.time() - _shadow["last_tick_ts"]) > 120)
        return
    if instrument_key != _shadow["option_key"]:
        return

    # Freak-tick damper: ignore one isolated >15% jump — a real repricing
    # shows up on the next tick too. 2026-07-17 shadow peaked +70.6pt on a
    # spike the main feed's filter would have dropped, inflating the lock
    # floor and the recorded exit.
    prev = _shadow["last_price"]
    if prev and abs(ltp - prev) / prev > 0.15 and not _shadow.get("suspect_seen"):
        _shadow["suspect_seen"] = True
        return
    _shadow["suspect_seen"] = False

    _shadow["last_price"]   = ltp
    _shadow["last_tick_ts"] = time.time()
    if ltp > _shadow["peak"]:
        _shadow["peak"] = ltp

    entry     = _shadow["entry_price"]
    peak_gain = _shadow["peak"] - entry
    if ltp <= entry - _STOP_PTS:
        _close(ltp, "stop")
    elif peak_gain >= _LOCK_ARM_PTS:
        keep  = _LOCK_BIG_KEEP if peak_gain >= _LOCK_BIG_PTS else _LOCK_KEEP
        floor = entry + peak_gain * keep
        if ltp <= floor:
            _close(ltp, "profit_lock")


def _close(exit_price: float, reason: str, stale: bool = False) -> None:
    global _shadow, _cooldown_until
    s, _shadow = _shadow, None
    _cooldown_until = time.time() + _COOLDOWN_S
    pnl = round(exit_price - s["entry_price"], 1)
    row = {
        "event":       "close",
        "ts":          datetime.now(_IST).isoformat(),
        "id":          s["id"],
        "exit_price":  round(exit_price, 1),
        "exit_reason": reason,
        "pnl_pts":     pnl,
        "peak_pts":    round(s["peak"] - s["entry_price"], 1),
        "held_secs":   round(time.time() - s["entry_ts"]),
    }
    if stale:
        row["stale_price"] = True
    _append(row)
    log.info("[shadow] CLOSE %s %s @ %.1f (%s) pnl %+.1fpt peak %+.1fpt",
             s["entry_type"], s["option"], exit_price, reason,
             pnl, s["peak"] - s["entry_price"])
