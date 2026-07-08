"""
IV Rank tracker — keeps rolling history of ATM IV and computes percentile rank.

IV Rank = (current_iv - low) / (high - low) × 100 against the baseline pool.
High IV rank (>70) means options are expensive → buying has poor expected value.

The baseline pool is GLOBAL and persisted to disk (logs/iv_history.json):
per-contract histories reset every session/expiry, and ranking against a few
hours of one contract's IV made the rank swing 70-100% on 0.005 wiggles —
it blocked 22 strong entries on 2026-07-07 alone. Contract keys change with
every expiry, so persistence only makes sense for the pooled ATM-IV series.
"""
from __future__ import annotations

import json
import logging
import time as _time
from collections import deque
from pathlib import Path

log = logging.getLogger(__name__)

_MAXLEN        = 30 * 375   # per-key session history cap
_GLOBAL_MAXLEN = 10 * 375   # ~10 trading days of pooled ATM IV samples
_HIGH_RANK_THR = 70.0       # block buys above this
_LOW_RANK_THR  = 20.0       # bonus: IV cheap, prefer buying
_MIN_POOL      = 400        # pooled samples needed before the pool drives rank

_FILE = Path("logs/iv_history.json")
_SAVE_EVERY_SECS = 120.0

_iv_history: dict[str, deque[float]] = {}
_global_pool: deque = deque(maxlen=_GLOBAL_MAXLEN)
_last_save: float = 0.0


def _load_pool() -> None:
    if not _FILE.exists():
        return
    try:
        data = json.loads(_FILE.read_text(encoding="utf-8"))
        if isinstance(data, list):
            for v in data[-_GLOBAL_MAXLEN:]:
                _global_pool.append(float(v))
        log.info("IV pool loaded: %d samples", len(_global_pool))
    except Exception as e:
        log.error("iv_history load: %s", e)


def _maybe_save_pool() -> None:
    global _last_save
    now = _time.monotonic()
    if now - _last_save < _SAVE_EVERY_SECS:
        return
    _last_save = now
    try:
        _FILE.parent.mkdir(exist_ok=True)
        _FILE.write_text(json.dumps([round(v, 4) for v in _global_pool]), encoding="utf-8")
    except Exception as e:
        log.error("iv_history save: %s", e)


def update(instrument_key: str, iv: float) -> None:
    if not iv:
        return
    # Upstox sends IV in mixed units — WebSocket marketFF.iv as a fraction
    # (0.12 = 12%) but chain REST option_greeks.iv as a percent (12.13).
    # A history mixing both units pins the rank near 0 for every fraction
    # value (0.12 vs a 12.13 max), which made the Premium Decay Guard fire
    # on noise. Normalize everything to fraction; index ATM IV never
    # legitimately exceeds 100%, so >1.0 reliably means percent.
    if iv > 1.0:
        iv /= 100.0
    if instrument_key not in _iv_history:
        _iv_history[instrument_key] = deque(maxlen=_MAXLEN)
    _iv_history[instrument_key].append(iv)
    _global_pool.append(iv)
    _maybe_save_pool()


def get_iv_rank(instrument_key: str) -> float | None:
    h = list(_iv_history.get(instrument_key, []))
    if len(h) < 20:
        return None
    # Rank against the multi-day pooled baseline when it has enough depth —
    # a session-only baseline made rank swing 70-100% on noise
    baseline = list(_global_pool) if len(_global_pool) >= _MIN_POOL else h
    lo, hi = min(baseline), max(baseline)
    if hi <= lo:
        return 50.0
    # A <10% relative range carries no information: report neutral instead.
    if (hi - lo) / lo < 0.10:
        return 50.0
    rank = (h[-1] - lo) / (hi - lo) * 100
    return round(min(100.0, max(0.0, rank)), 1)


def get_current_iv(instrument_key: str) -> float | None:
    h = _iv_history.get(instrument_key)
    return h[-1] if h else None


def is_high_iv(instrument_key: str) -> bool:
    rank = get_iv_rank(instrument_key)
    return rank is not None and rank > _HIGH_RANK_THR


def is_low_iv(instrument_key: str) -> bool:
    rank = get_iv_rank(instrument_key)
    return rank is not None and rank < _LOW_RANK_THR


def get_status(instrument_key: str) -> dict:
    rank = get_iv_rank(instrument_key)
    iv   = get_current_iv(instrument_key)
    return {
        "iv":        round(iv * 100, 1) if iv else None,   # display as %
        "iv_rank":   rank,
        "high_iv":   is_high_iv(instrument_key),
        "low_iv":    is_low_iv(instrument_key),
        "label":     (
            "HIGH — avoid buying" if rank is not None and rank > _HIGH_RANK_THR else
            "LOW — good to buy"  if rank is not None and rank < _LOW_RANK_THR  else
            "NORMAL"             if rank is not None else "—"
        ),
    }


_load_pool()
