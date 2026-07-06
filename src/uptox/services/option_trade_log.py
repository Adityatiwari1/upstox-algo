"""
Per-trade option tick recorder — logs price/IV over the life of each open
trade so the real price path can be reconstructed afterward.

Why this exists: market_data.jsonl only samples the *current ATM strike's*
CE/PE ltp every 5min. Once ATM rolls to a new strike, the old strike's price
vanishes from that log — even while we still hold it. Found 2026-07-06: a
CE 24400 trade exited early, and a broker chart later showed that same
contract ran to ~92 hours afterward, invisible to our own market-wide log.
This recorder tracks the *specific contract we're holding*, independent of
which strike is currently ATM.
"""
from __future__ import annotations

import json
import logging
import time as _time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

_IST = ZoneInfo("Asia/Kolkata")
_DIR = Path("logs/option_ticks")

_MIN_INTERVAL = 2.0   # seconds between samples per trade — enough resolution
                      # to reconstruct the price path without writing on every tick
_last_log_ts: dict[str, float] = {}


def record_tick(trade_id: str, price: float, iv: float, pnl_pts: float) -> None:
    """Append one price/IV sample for the given open trade, throttled to
    once per _MIN_INTERVAL seconds so active ticks don't spam disk."""
    if not trade_id or not price:
        return
    now = _time.monotonic()
    if now - _last_log_ts.get(trade_id, 0.0) < _MIN_INTERVAL:
        return
    _last_log_ts[trade_id] = now
    try:
        _DIR.mkdir(parents=True, exist_ok=True)
        p = _DIR / f"{trade_id}.jsonl"
        rec = {
            "ts":      datetime.now(_IST).isoformat(timespec="seconds"),
            "price":   round(price, 2),
            "iv":      round(iv * 100, 2) if 0 < iv < 1 else (round(iv, 2) if iv else None),
            "pnl_pts": round(pnl_pts, 2),
        }
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as e:
        log.error("option_trade_log record error: %s", e)


def get_ticks(trade_id: str) -> list[dict]:
    """Read back all recorded samples for a trade — for post-hoc analysis."""
    p = _DIR / f"{trade_id}.jsonl"
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out
