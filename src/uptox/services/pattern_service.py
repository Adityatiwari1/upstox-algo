"""
Candlestick pattern detection — delegates to candle_patterns engine.
Kept for backward-compat with TechnicalPanel import.
"""
from __future__ import annotations
from uptox.services.sma_service import TimeFrame
from uptox.services.candle_patterns import detect_for, PatternResult


def detect_str(instrument_key: str, tf: TimeFrame = TimeFrame.M5) -> str:
    """Single-string summary of most significant pattern (backward compat)."""
    results = detect_for(instrument_key, tf)
    if not results:
        return "—"
    top = results[0]
    icon = "▲" if top.bias == "BULL" else "▼" if top.bias == "BEAR" else "●"
    return f"{icon} {top.name}"


def detect_all(instrument_key: str, tf: TimeFrame = TimeFrame.M5) -> list[PatternResult]:
    """All patterns found on the last 5 candles."""
    return detect_for(instrument_key, tf)


# Keep old name so existing callers work
def detect(instrument_key: str, tf: TimeFrame = TimeFrame.M5) -> str:
    return detect_str(instrument_key, tf)
