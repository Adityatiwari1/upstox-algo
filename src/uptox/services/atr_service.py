"""ATR (Average True Range) on OHLC candle history from sma_service."""
from __future__ import annotations
from uptox.services.sma_service import TimeFrame


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
