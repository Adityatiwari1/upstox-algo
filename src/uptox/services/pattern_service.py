"""
Candlestick pattern detection from the last N closed OHLCV bars.
Reads from sma_service.get_last_candles() — no external API calls.
"""
from __future__ import annotations
from uptox.services.sma_service import TimeFrame


def detect(instrument_key: str, tf: TimeFrame = TimeFrame.M5) -> str:
    from uptox.services import sma_service
    candles = sma_service.get_last_candles(instrument_key, tf, n=3)
    if len(candles) < 2:
        return "—"

    o0, h0, l0, c0 = candles[-1]   # most recent closed bar
    o1, h1, l1, c1 = candles[-2]   # bar before it

    body0  = abs(c0 - o0)
    range0 = h0 - l0 or 1e-9       # avoid div/0
    upper0 = h0 - max(o0, c0)
    lower0 = min(o0, c0) - l0

    bull0 = c0 > o0
    bull1 = c1 > o1

    # ── single-bar patterns ──────────────────────────────────────────────
    if body0 / range0 < 0.10:
        return "Doji"

    if lower0 > 2 * body0 and upper0 < body0:
        return "Hammer" if not bull1 else "Hanging Man"

    if upper0 > 2 * body0 and lower0 < body0:
        return "Shooting Star" if bull1 else "Inverted Hammer"

    # ── two-bar patterns ────────────────────────────────────────────────
    # Bullish Engulfing
    if (not bull1 and bull0
            and o0 <= c1 and c0 >= o1
            and body0 > abs(c1 - o1)):
        return "Bull Engulf"

    # Bearish Engulfing
    if (bull1 and not bull0
            and o0 >= c1 and c0 <= o1
            and body0 > abs(c1 - o1)):
        return "Bear Engulf"

    # Inside Bar (consolidation)
    if h0 < h1 and l0 > l1:
        return "Inside Bar"

    # ── momentum labels ─────────────────────────────────────────────────
    if bull0 and body0 / range0 > 0.65:
        return "Strong Bull"
    if not bull0 and body0 / range0 > 0.65:
        return "Strong Bear"

    return "—"
