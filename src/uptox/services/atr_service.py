"""ATR (Average True Range) on OHLC candle history from sma_service."""
from __future__ import annotations
import logging

from uptox.services.sma_service import TimeFrame

log = logging.getLogger(__name__)

_NIFTY = "NSE_INDEX|Nifty 50"
_last_regime: str = ""   # last regime returned for Nifty — log shifts only

# Regime thresholds (Nifty 5-min ATR14, Nifty points)
_ATR_TRENDING     = 35.0   # above → trending
_ATR_CONSOLIDATING = 20.0  # below → consolidating

# IV thresholds (option IV %)
_IV_TRENDING      = 18.0
_IV_CONSOLIDATING = 13.0


def min_sl_pts(entry_price: float) -> float:
    """Minimum absolute SL distance in option points, tiered by entry price.
    Prevents noise-triggered SL on cheap options where % SL is too tight.
    """
    if entry_price < 50:
        return 8.0
    elif entry_price < 100:
        return 12.0
    elif entry_price < 200:
        return 18.0
    else:
        return round(entry_price * 0.08, 1)


def get_regime(iv: float = 0.0, instrument_key: str = _NIFTY) -> dict:
    """
    Detect market regime from ATR + IV and return trading params.

    Returns dict with keys:
      regime   : "TRENDING" | "NORMAL" | "CONSOLIDATING"
      sl_pct   : SL as fraction of option entry price
      rr       : reward-to-risk ratio for target
      atr      : current ATR value used
    """
    atr = get_atr(instrument_key) or 25.0

    trending     = atr >= _ATR_TRENDING     or (iv > 0 and iv >= _IV_TRENDING)
    consolidating = atr <= _ATR_CONSOLIDATING and (iv <= 0 or iv < _IV_CONSOLIDATING)

    if trending:
        out = {"regime": "TRENDING",      "sl_pct": 0.10, "rr": 3.0, "atr": atr}
    elif consolidating:
        out = {"regime": "CONSOLIDATING", "sl_pct": 0.05, "rr": 1.5, "atr": atr}
    else:
        out = {"regime": "NORMAL",        "sl_pct": 0.08, "rr": 2.0, "atr": atr}

    global _last_regime
    if instrument_key == _NIFTY and out["regime"] != _last_regime:
        if _last_regime:
            log.info("Regime shift: %s → %s  (ATR=%.1f IV=%.1f)",
                     _last_regime, out["regime"], atr, iv)
        _last_regime = out["regime"]
    return out


def get_atr(instrument_key: str, tf: TimeFrame = TimeFrame.M5, period: int = 14) -> float | None:
    """Wilder ATR over `period` bars. Returns None if insufficient history."""
    from uptox.services import sma_service
    candles = sma_service.get_last_candles(instrument_key, tf, n=period + 2)
    if len(candles) < 2:
        return None
    trs: list[float] = []
    for i in range(1, len(candles)):
        h, l, pc = candles[i][1], candles[i][2], candles[i - 1][3]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    if not trs:
        return None
    use = trs[-period:]
    return round(sum(use) / len(use), 2)


def nifty_sl_points(instrument_key: str, tf: TimeFrame = TimeFrame.M5,
                    multiplier: float = 1.5, fallback: float = 30.0) -> float:
    """
    ATR-based Nifty point SL.
    multiplier=1.5 means SL = 1.5 × ATR(14) Nifty points.
    Falls back to fixed `fallback` pts if ATR unavailable.
    """
    atr = get_atr(instrument_key, tf)
    return round(atr * multiplier, 1) if atr else fallback
