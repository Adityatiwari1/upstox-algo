"""
Candlestick pattern detection on OHLC bars.

All functions take candles as list of (O, H, L, C) tuples (oldest first).
Returns list of PatternResult — name, bias (BULL/BEAR/NEUTRAL), strength (1-3).
"""
from __future__ import annotations
from dataclasses import dataclass

Candle = tuple[float, float, float, float]  # O, H, L, C


@dataclass
class PatternResult:
    name:     str
    bias:     str    # "BULL" | "BEAR" | "NEUTRAL"
    strength: int    # 1=weak  2=moderate  3=strong
    candles:  int    # how many candles used

    def __str__(self) -> str:
        icon = "▲" if self.bias == "BULL" else "▼" if self.bias == "BEAR" else "●"
        stars = "★" * self.strength
        return f"{icon} {self.name} {stars}"


# ── helpers ──────────────────────────────────────────────────────────────────

def _body(c: Candle) -> float:
    return abs(c[3] - c[0])

def _range(c: Candle) -> float:
    return c[1] - c[2] if c[1] != c[2] else 0.001

def _upper_wick(c: Candle) -> float:
    return c[1] - max(c[0], c[3])

def _lower_wick(c: Candle) -> float:
    return min(c[0], c[3]) - c[2]

def _is_bull(c: Candle) -> bool:
    return c[3] > c[0]

def _is_bear(c: Candle) -> bool:
    return c[3] < c[0]

def _body_pct(c: Candle) -> float:
    return _body(c) / _range(c)

def _midpoint(c: Candle) -> float:
    return (max(c[0], c[3]) + min(c[0], c[3])) / 2


# ── single-candle patterns ────────────────────────────────────────────────────

def _doji(c: Candle) -> PatternResult | None:
    if _body_pct(c) < 0.08:
        return PatternResult("Doji", "NEUTRAL", 1, 1)
    return None


def _hammer(c: Candle) -> PatternResult | None:
    b  = _body(c)
    lw = _lower_wick(c)
    uw = _upper_wick(c)
    rng = _range(c)
    if b > 0 and lw >= 2 * b and uw <= 0.3 * b and b / rng > 0.1:
        bias = "BULL"
        return PatternResult("Hammer", bias, 2, 1)
    return None


def _hanging_man(c: Candle) -> PatternResult | None:
    b  = _body(c)
    lw = _lower_wick(c)
    uw = _upper_wick(c)
    rng = _range(c)
    if b > 0 and lw >= 2 * b and uw <= 0.3 * b and b / rng > 0.1:
        return PatternResult("Hanging Man", "BEAR", 2, 1)
    return None


def _shooting_star(c: Candle) -> PatternResult | None:
    b  = _body(c)
    uw = _upper_wick(c)
    lw = _lower_wick(c)
    rng = _range(c)
    if b > 0 and uw >= 2 * b and lw <= 0.3 * b and b / rng > 0.1:
        return PatternResult("Shooting Star", "BEAR", 2, 1)
    return None


def _inverted_hammer(c: Candle) -> PatternResult | None:
    b  = _body(c)
    uw = _upper_wick(c)
    lw = _lower_wick(c)
    rng = _range(c)
    if b > 0 and uw >= 2 * b and lw <= 0.3 * b and b / rng > 0.1:
        return PatternResult("Inverted Hammer", "BULL", 2, 1)
    return None


def _marubozu(c: Candle) -> PatternResult | None:
    rng = _range(c)
    uw  = _upper_wick(c)
    lw  = _lower_wick(c)
    if uw <= 0.02 * rng and lw <= 0.02 * rng and _body_pct(c) > 0.90:
        bias = "BULL" if _is_bull(c) else "BEAR"
        return PatternResult(
            "Bullish Marubozu" if bias == "BULL" else "Bearish Marubozu",
            bias, 3, 1,
        )
    return None


def _spinning_top(c: Candle) -> PatternResult | None:
    bp = _body_pct(c)
    uw = _upper_wick(c)
    lw = _lower_wick(c)
    if 0.08 <= bp <= 0.35 and uw > 0.2 * _range(c) and lw > 0.2 * _range(c):
        return PatternResult("Spinning Top", "NEUTRAL", 1, 1)
    return None


def _belt_hold(c: Candle) -> PatternResult | None:
    rng = _range(c)
    if _is_bull(c) and _lower_wick(c) <= 0.02 * rng and _body_pct(c) > 0.6:
        return PatternResult("Bullish Belt Hold", "BULL", 2, 1)
    if _is_bear(c) and _upper_wick(c) <= 0.02 * rng and _body_pct(c) > 0.6:
        return PatternResult("Bearish Belt Hold", "BEAR", 2, 1)
    return None


# ── two-candle patterns ───────────────────────────────────────────────────────

def _engulfing(prev: Candle, curr: Candle) -> PatternResult | None:
    if (_is_bear(prev) and _is_bull(curr)
            and curr[0] <= prev[3] and curr[3] >= prev[0]):
        return PatternResult("Bullish Engulfing", "BULL", 3, 2)
    if (_is_bull(prev) and _is_bear(curr)
            and curr[0] >= prev[3] and curr[3] <= prev[0]):
        return PatternResult("Bearish Engulfing", "BEAR", 3, 2)
    return None


def _harami(prev: Candle, curr: Candle) -> PatternResult | None:
    p_hi = max(prev[0], prev[3])
    p_lo = min(prev[0], prev[3])
    c_hi = max(curr[0], curr[3])
    c_lo = min(curr[0], curr[3])
    if c_hi < p_hi and c_lo > p_lo:
        if _is_bear(prev) and _is_bull(curr):
            return PatternResult("Bullish Harami", "BULL", 2, 2)
        if _is_bull(prev) and _is_bear(curr):
            return PatternResult("Bearish Harami", "BEAR", 2, 2)
    return None


def _tweezer(prev: Candle, curr: Candle) -> PatternResult | None:
    tol = _range(prev) * 0.05
    if abs(prev[2] - curr[2]) <= tol and _is_bear(prev) and _is_bull(curr):
        return PatternResult("Tweezer Bottom", "BULL", 2, 2)
    if abs(prev[1] - curr[1]) <= tol and _is_bull(prev) and _is_bear(curr):
        return PatternResult("Tweezer Top", "BEAR", 2, 2)
    return None


def _piercing_dark_cloud(prev: Candle, curr: Candle) -> PatternResult | None:
    mid = _midpoint(prev)
    if (_is_bear(prev) and _is_bull(curr)
            and curr[0] < prev[3] and curr[3] > mid and curr[3] < prev[0]):
        return PatternResult("Piercing Line", "BULL", 2, 2)
    if (_is_bull(prev) and _is_bear(curr)
            and curr[0] > prev[3] and curr[3] < mid and curr[3] > prev[0]):
        return PatternResult("Dark Cloud Cover", "BEAR", 2, 2)
    return None


def _on_neck(prev: Candle, curr: Candle) -> PatternResult | None:
    tol = _range(prev) * 0.05
    if (_is_bear(prev) and _is_bull(curr)
            and abs(curr[3] - prev[3]) <= tol):
        return PatternResult("On Neck (Bear)", "BEAR", 1, 2)
    return None


# ── three-candle patterns ─────────────────────────────────────────────────────

def _morning_evening_star(
    c1: Candle, c2: Candle, c3: Candle
) -> PatternResult | None:
    small = _body_pct(c2) < 0.3
    if _is_bear(c1) and small and _is_bull(c3) and c3[3] > _midpoint(c1):
        return PatternResult("Morning Star", "BULL", 3, 3)
    if _is_bull(c1) and small and _is_bear(c3) and c3[3] < _midpoint(c1):
        return PatternResult("Evening Star", "BEAR", 3, 3)
    return None


def _three_soldiers_crows(
    c1: Candle, c2: Candle, c3: Candle
) -> PatternResult | None:
    if (_is_bull(c1) and _is_bull(c2) and _is_bull(c3)
            and c2[3] > c1[3] and c3[3] > c2[3]
            and c2[0] > c1[0] and c3[0] > c2[0]):
        return PatternResult("Three White Soldiers", "BULL", 3, 3)
    if (_is_bear(c1) and _is_bear(c2) and _is_bear(c3)
            and c2[3] < c1[3] and c3[3] < c2[3]
            and c2[0] < c1[0] and c3[0] < c2[0]):
        return PatternResult("Three Black Crows", "BEAR", 3, 3)
    return None


def _three_inside(c1: Candle, c2: Candle, c3: Candle) -> PatternResult | None:
    if (_is_bear(c1) and _is_bull(c2)
            and max(c2[0], c2[3]) < max(c1[0], c1[3])
            and min(c2[0], c2[3]) > min(c1[0], c1[3])
            and _is_bull(c3) and c3[3] > c1[0]):
        return PatternResult("Three Inside Up", "BULL", 3, 3)
    if (_is_bull(c1) and _is_bear(c2)
            and max(c2[0], c2[3]) < max(c1[0], c1[3])
            and min(c2[0], c2[3]) > min(c1[0], c1[3])
            and _is_bear(c3) and c3[3] < c1[0]):
        return PatternResult("Three Inside Down", "BEAR", 3, 3)
    return None


def _three_outside(c1: Candle, c2: Candle, c3: Candle) -> PatternResult | None:
    if (_is_bear(c1) and _is_bull(c2)
            and c2[0] <= c1[3] and c2[3] >= c1[0]   # engulfs
            and _is_bull(c3) and c3[3] > c2[3]):
        return PatternResult("Three Outside Up", "BULL", 3, 3)
    if (_is_bull(c1) and _is_bear(c2)
            and c2[0] >= c1[3] and c2[3] <= c1[0]
            and _is_bear(c3) and c3[3] < c2[3]):
        return PatternResult("Three Outside Down", "BEAR", 3, 3)
    return None


def _abandoned_baby(c1: Candle, c2: Candle, c3: Candle) -> PatternResult | None:
    """Doji gapping away from both neighbours."""
    if _body_pct(c2) < 0.08:   # c2 is doji
        if (_is_bear(c1) and c2[1] < c1[2]   # doji gaps down from c1
                and _is_bull(c3) and c3[0] > c2[1]):  # c3 gaps up from doji
            return PatternResult("Abandoned Baby (Bull)", "BULL", 3, 3)
        if (_is_bull(c1) and c2[0] > c1[1]
                and _is_bear(c3) and c3[0] < c2[2]):
            return PatternResult("Abandoned Baby (Bear)", "BEAR", 3, 3)
    return None


# ── main detector ─────────────────────────────────────────────────────────────

def detect(candles: list[Candle]) -> list[PatternResult]:
    """
    Run all pattern checks on the last 3 candles.
    Returns list of PatternResult (may be empty), sorted by strength desc.
    """
    results: list[PatternResult] = []

    if not candles:
        return results

    c = candles[-1]   # current (most recent closed bar)

    # ── single candle (using context to distinguish hammer vs hanging man etc.)
    # Need at least 2 prior bars to know trend direction
    prev_closes = [x[3] for x in candles[:-1]]
    uptrend   = len(prev_closes) >= 2 and prev_closes[-1] > prev_closes[-2]
    downtrend = len(prev_closes) >= 2 and prev_closes[-1] < prev_closes[-2]

    for fn in (_doji, _marubozu, _belt_hold, _spinning_top):
        r = fn(c)
        if r:
            results.append(r)

    r = _hammer(c)
    if r and downtrend:
        results.append(r)

    r = _hanging_man(c)
    if r and uptrend:
        results.append(r)

    r = _shooting_star(c)
    if r and uptrend:
        results.append(r)

    r = _inverted_hammer(c)
    if r and downtrend:
        results.append(r)

    # ── two candle
    if len(candles) >= 2:
        p = candles[-2]
        for fn in (_engulfing, _harami, _tweezer, _piercing_dark_cloud, _on_neck):
            r = fn(p, c)
            if r:
                results.append(r)

    # ── three candle
    if len(candles) >= 3:
        c1, c2, c3 = candles[-3], candles[-2], candles[-1]
        for fn in (
            _morning_evening_star,
            _three_soldiers_crows,
            _three_inside,
            _three_outside,
            _abandoned_baby,
        ):
            r = fn(c1, c2, c3)
            if r:
                results.append(r)

    # Deduplicate by name, keep highest strength
    seen: dict[str, PatternResult] = {}
    for r in results:
        if r.name not in seen or r.strength > seen[r.name].strength:
            seen[r.name] = r

    return sorted(seen.values(), key=lambda x: -x.strength)


def detect_for(instrument_key: str, tf=None) -> list[PatternResult]:
    """Convenience wrapper — reads candles from sma_service."""
    from uptox.services import sma_service
    from uptox.services.sma_service import TimeFrame
    tf = tf or TimeFrame.M5
    candles = sma_service.get_last_candles(instrument_key, tf, n=5)
    return detect(candles)
