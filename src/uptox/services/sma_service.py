"""
SMA service — multi-timeframe (5min, 15min, 1hr, 4hr).
Historical prime via Upstox candle API + real-time update per tick.

Upstox base intervals used:
  1min  candles  →  aggregated to 5min / 15min
  30min candles  →  aggregated to 1hr  / 4hr
"""
from __future__ import annotations

import asyncio
import httpx
from collections import deque
from datetime import date, timedelta, datetime
from enum import Enum
from urllib.parse import quote
from zoneinfo import ZoneInfo

PERIODS = (10, 20, 50, 100, 200)
_MAX = max(PERIODS)

IST = ZoneInfo("Asia/Kolkata")

_HIST_URL = (
    "https://api.upstox.com/v2/historical-candle"
    "/{key}/{interval}/{to}/{frm}"
)


class TimeFrame(str, Enum):
    M5  = "5min"
    M15 = "15min"
    H1  = "1hr"
    H4  = "4hr"


ALL_TFS = [TimeFrame.M5, TimeFrame.M15, TimeFrame.H1, TimeFrame.H4]

# Minutes per aggregated bar
_TF_MINUTES: dict[TimeFrame, int] = {
    TimeFrame.M5:  5,
    TimeFrame.M15: 15,
    TimeFrame.H1:  60,
    TimeFrame.H4:  240,
}

# Upstox interval to fetch as base resolution
_BASE_INTERVAL: dict[TimeFrame, str] = {
    TimeFrame.M5:  "1minute",
    TimeFrame.M15: "1minute",
    TimeFrame.H1:  "30minute",
    TimeFrame.H4:  "30minute",
}

# Calendar days to fetch; sized to cover SMA 200 bars with buffer
_FETCH_DAYS: dict[TimeFrame, int] = {
    TimeFrame.M5:  10,    # ~5 trading days → 375 one-min bars/day → 1875 bars
    TimeFrame.M15: 25,    # ~15 trading days → plenty for 200 fifteen-min bars
    TimeFrame.H1:  80,    # ~55 trading days
    TimeFrame.H4:  300,   # ~200 trading days
}

# (instrument_key, tf) → deque of closed bar closes, maxlen=_MAX
_closes: dict[tuple[str, TimeFrame], deque] = {}
# (instrument_key, tf) → {period: sma | None}
_smas:   dict[tuple[str, TimeFrame], dict[int, float | None]] = {}


# ── helpers ──────────────────────────────────────────────────────────────────

def _floor_to(dt: datetime, interval_min: int) -> datetime:
    """Floor datetime to nearest interval_min boundary (within the day)."""
    total = dt.hour * 60 + dt.minute
    floored = (total // interval_min) * interval_min
    return dt.replace(hour=floored // 60, minute=floored % 60,
                      second=0, microsecond=0)


def _aggregate(raw_oldest_first: list, interval_min: int) -> list[float]:
    """
    Aggregate raw candles [[ts_iso, o, h, l, c, v, oi], ...] (oldest first)
    into interval_min-minute bars. Returns list of bar closes, oldest first.
    """
    bars: dict[datetime, list[float]] = {}
    for row in raw_oldest_first:
        ts = datetime.fromisoformat(row[0]).astimezone(IST)
        bar_ts = _floor_to(ts, interval_min)
        c = float(row[4])
        if bar_ts not in bars:
            # [open, high, low, close]
            bars[bar_ts] = [float(row[1]), float(row[2]), float(row[3]), c]
        else:
            bars[bar_ts][1] = max(bars[bar_ts][1], float(row[2]))
            bars[bar_ts][2] = min(bars[bar_ts][2], float(row[3]))
            bars[bar_ts][3] = c  # last tick = close of bar
    return [v[3] for _, v in sorted(bars.items())]


def _compute(closes: list[float]) -> dict[int, float | None]:
    n = len(closes)
    return {p: (sum(closes[-p:]) / p if n >= p else None) for p in PERIODS}


# ── API fetch ─────────────────────────────────────────────────────────────────

async def _fetch_raw(
    instrument_key: str, interval: str, days_back: int, access_token: str
) -> list:
    to_d  = date.today()
    frm_d = to_d - timedelta(days=days_back)
    url   = _HIST_URL.format(
        key=quote(instrument_key, safe=""),
        interval=interval,
        to=to_d.isoformat(),
        frm=frm_d.isoformat(),
    )
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                url,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Accept": "application/json",
                },
            )
        if resp.status_code != 200:
            print(f"[sma] hist {interval} {resp.status_code} for {instrument_key}: {resp.text[:200]}")
            return []
        return resp.json().get("data", {}).get("candles", [])
    except Exception as e:
        print(f"[sma] fetch error {instrument_key} {interval}: {e}")
        return []


# ── public API ────────────────────────────────────────────────────────────────

async def fetch_and_prime(instrument_key: str, access_token: str) -> None:
    """Fetch candles for all timeframes in parallel and prime SMA history."""

    async def _prime_tf(tf: TimeFrame) -> None:
        raw = await _fetch_raw(
            instrument_key,
            _BASE_INTERVAL[tf],
            _FETCH_DAYS[tf],
            access_token,
        )
        if not raw:
            return
        oldest_first = list(reversed(raw))   # Upstox returns newest-first
        closes = _aggregate(oldest_first, _TF_MINUTES[tf])
        k = (instrument_key, tf)
        _closes[k] = deque(closes[-_MAX:], maxlen=_MAX)
        _smas[k]   = _compute(list(_closes[k]))
        sma200 = _smas[k].get(200)
        sma200_str = f"{sma200:.1f}" if sma200 else "—"
        print(f"[sma] {instrument_key} {tf.value}: {len(closes)} bars  SMA200={sma200_str}")

    await asyncio.gather(*(_prime_tf(tf) for tf in ALL_TFS))


def update_realtime(instrument_key: str, ltp: float) -> None:
    """
    Recompute SMAs for every timeframe using stored history + LTP as the
    live (not-yet-closed) bar's close. Called on every incoming tick.
    """
    if not ltp:
        return
    for tf in ALL_TFS:
        k = (instrument_key, tf)
        if k not in _closes:
            continue
        closes = list(_closes[k]) + [ltp]
        _smas[k] = _compute(closes)


def get_smas(instrument_key: str, tf: TimeFrame) -> dict[int, float | None]:
    return _smas.get((instrument_key, tf), {p: None for p in PERIODS})


def get_sma(instrument_key: str, tf: TimeFrame, period: int) -> float | None:
    return _smas.get((instrument_key, tf), {}).get(period)
