"""
IV Rank tracker — keeps rolling history of ATM IV and computes percentile rank.

IV Rank = (current_iv - 30d_low) / (30d_high - 30d_low) × 100
High IV rank (>70) means options are expensive → buying has poor expected value.
"""
from __future__ import annotations
from collections import deque

_MAXLEN        = 30 * 375   # 30 trading days × ~375 ticks/day
_HIGH_RANK_THR = 70.0       # block buys above this
_LOW_RANK_THR  = 20.0       # bonus: IV cheap, prefer buying

_iv_history: dict[str, deque[float]] = {}


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


def get_iv_rank(instrument_key: str) -> float | None:
    h = list(_iv_history.get(instrument_key, []))
    if len(h) < 20:
        return None
    lo, hi = min(h), max(h)
    if hi <= lo:
        return 50.0
    return round((h[-1] - lo) / (hi - lo) * 100, 1)


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
