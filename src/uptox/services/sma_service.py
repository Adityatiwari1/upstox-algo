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
from datetime import date, timedelta, datetime, time as dt_time
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
# Historical API excludes the current trading day — intraday endpoint fills it.
# V3: /{unit}/{interval} e.g. minutes/1, minutes/30
_INTRA_URL = "https://api.upstox.com/v3/historical-candle/intraday/{key}/{unit}/{interval}"


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
    TimeFrame.M5:  15,    # 15 cal days → 7-8 trading days, guarantees 5 even with holidays
    TimeFrame.M15: 25,    # ~15 trading days → plenty for 200 fifteen-min bars
    TimeFrame.H1:  80,    # ~55 trading days
    TimeFrame.H4:  180,   # fetched in 2×90-day chunks (Upstox 30min limit ~93 days)
}

# Candle = (open, high, low, close)
Candle = tuple[float, float, float, float]

# (instrument_key, tf) → deque of closed OHLCV bars, maxlen=_MAX
_candles: dict[tuple[str, TimeFrame], deque] = {}
# (instrument_key, tf) → {period: sma | None}
_smas:    dict[tuple[str, TimeFrame], dict[int, float | None]] = {}


# ── helpers ──────────────────────────────────────────────────────────────────

def _floor_to(dt: datetime, interval_min: int) -> datetime:
    """Floor datetime to nearest interval_min boundary (within the day)."""
    total = dt.hour * 60 + dt.minute
    floored = (total // interval_min) * interval_min
    return dt.replace(hour=floored // 60, minute=floored % 60,
                      second=0, microsecond=0)


_MARKET_OPEN = dt_time(9, 15)  # NSE regular session start


def _aggregate(
    raw_oldest_first: list, interval_min: int, exclude_from: datetime | None = None
) -> list[Candle]:
    """
    Aggregate raw candles [[ts_iso, o, h, l, c, v, oi], ...] (oldest first)
    into interval_min-minute bars. Returns list of (O,H,L,C) tuples, oldest first.
    Pre-market ticks (before 09:15 IST) are excluded so bars align with NSE open.
    exclude_from drops bars at/after that boundary — used to leave the currently
    forming bar out of primed history (live ticks rebuild it, avoiding a duplicate).
    """
    bars: dict[datetime, list[float]] = {}
    for row in raw_oldest_first:
        ts = datetime.fromisoformat(row[0]).astimezone(IST)
        if ts.time() < _MARKET_OPEN:   # skip pre-open (09:00–09:14)
            continue
        bar_ts = _floor_to(ts, interval_min)
        if exclude_from is not None and bar_ts >= exclude_from:
            continue
        if bar_ts not in bars:
            bars[bar_ts] = [float(row[1]), float(row[2]), float(row[3]), float(row[4])]
        else:
            bars[bar_ts][1] = max(bars[bar_ts][1], float(row[2]))
            bars[bar_ts][2] = min(bars[bar_ts][2], float(row[3]))
            bars[bar_ts][3] = float(row[4])
    return [(v[0], v[1], v[2], v[3]) for _, v in sorted(bars.items())]


def _compute(candles: list[Candle]) -> dict[int, float | None]:
    closes = [c[3] for c in candles]
    n = len(closes)
    return {p: (sum(closes[-p:]) / p if n >= p else None) for p in PERIODS}


# ── API fetch ─────────────────────────────────────────────────────────────────

_30MIN_CHUNK = 90  # Upstox rejects 30minute requests beyond ~93 calendar days

import logging as _logging
_log = _logging.getLogger(__name__)


async def _fetch_range(
    instrument_key: str, interval: str, to_d: date, frm_d: date, access_token: str
) -> list:
    url = _HIST_URL.format(
        key=quote(instrument_key, safe=""),
        interval=interval,
        to=to_d.isoformat(),
        frm=frm_d.isoformat(),
    )
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                url,
                headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
            )
        if resp.status_code != 200:
            _log.warning("hist %s %s for %s: %s", interval, resp.status_code, instrument_key, resp.text[:200])
            return []
        return resp.json().get("data", {}).get("candles", [])
    except Exception as e:
        _log.error("fetch %s %s: %s", instrument_key, interval, e)
        return []


async def _fetch_intraday(
    instrument_key: str, unit: str, interval: str, access_token: str
) -> list:
    """Today's candles (V3 API) — the plain historical endpoint stops at yesterday's close."""
    url = _INTRA_URL.format(key=quote(instrument_key, safe=""), unit=unit, interval=interval)
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                url,
                headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
            )
        if resp.status_code != 200:
            _log.warning("intraday %s/%s %s for %s: %s",
                         unit, interval, resp.status_code, instrument_key, resp.text[:200])
            return []
        return resp.json().get("data", {}).get("candles", [])
    except Exception as e:
        _log.error("intraday fetch %s %s/%s: %s", instrument_key, unit, interval, e)
        return []


async def _fetch_raw(
    instrument_key: str, interval: str, days_back: int, access_token: str
) -> list:
    today = date.today()
    if interval == "30minute" and days_back > _30MIN_CHUNK:
        # Fetch in chunks and merge (Upstox rejects >~93-day range for 30min)
        all_rows: list = []
        cur_to = today
        remaining = days_back
        while remaining > 0:
            chunk = min(_30MIN_CHUNK, remaining)
            cur_frm = cur_to - timedelta(days=chunk)
            rows = await _fetch_range(instrument_key, interval, cur_to, cur_frm, access_token)
            all_rows.extend(rows)
            cur_to = cur_frm - timedelta(days=1)
            remaining -= chunk
        return all_rows
    return await _fetch_range(instrument_key, interval, today, today - timedelta(days=days_back), access_token)


# ── public API ────────────────────────────────────────────────────────────────

async def fetch_and_prime(instrument_key: str, access_token: str) -> None:
    """
    Fetch two base resolutions in parallel, then aggregate each into its TFs.
      1-min  × 15 cal-days  →  M5  + M15   (same raw data, one API call)
      30-min × 180 cal-days →  H1  + H4    (same raw data, one API call)
    """

    def _prime_from(raw: list, tf: TimeFrame) -> None:
        if not raw:
            _log.warning("prime %s %s: got 0 candles — historical data unavailable",
                         instrument_key, tf.value)
            return
        oldest_first = list(reversed(raw))   # Upstox returns newest-first
        # Leave the currently forming bar out — live ticks rebuild it after prime
        cutoff = _floor_to(datetime.now(IST), _TF_MINUTES[tf])
        bars = _aggregate(oldest_first, _TF_MINUTES[tf], exclude_from=cutoff)
        k = (instrument_key, tf)
        _candles[k] = deque(bars[-_MAX:], maxlen=_MAX)
        _smas[k]    = _compute(list(_candles[k]))
        sma200 = _smas[k].get(200)
        _log.info("%s %s: %d bars  SMA200=%s", instrument_key, tf.value, len(bars),
                  f"{sma200:.1f}" if sma200 else "—")

    raw_1min, raw_30min, intra_1min, intra_30min = await asyncio.gather(
        _fetch_raw(instrument_key, "1minute",  15,  access_token),
        _fetch_raw(instrument_key, "30minute", 180, access_token),
        _fetch_intraday(instrument_key, "minutes", "1",  access_token),
        _fetch_intraday(instrument_key, "minutes", "30", access_token),
    )
    # Merge today's intraday candles with multi-day history. Both lists are
    # newest-first; _aggregate keys bars by timestamp so order/overlap is safe.
    raw_1min  = intra_1min  + raw_1min
    raw_30min = intra_30min + raw_30min

    # M5 and M15 share the same 1-min fetch — perfectly in sync, no extra API call
    _prime_from(raw_1min, TimeFrame.M5)
    _prime_from(raw_1min, TimeFrame.M15)

    # H1 and H4 share the same 30-min fetch
    _prime_from(raw_30min, TimeFrame.H1)
    _prime_from(raw_30min, TimeFrame.H4)

    # Drop any live bar formed before this prime — appending it onto the
    # freshly loaded history would duplicate/corrupt the last bar.
    for tf in ALL_TFS:
        _live_bar.pop((instrument_key, tf), None)


# (instrument_key, tf) → [bar_start_dt, open, high, low, close] for the
# currently-forming bar. When a tick lands in a new bar period the previous
# live bar is appended to _candles as a closed bar.
_live_bar: dict[tuple[str, TimeFrame], list] = {}


def update_realtime(instrument_key: str, ltp: float) -> None:
    """
    Build live bars from ticks and recompute SMAs on every tick.

    Each timeframe keeps a forming bar (O/H/L/C accumulated from ticks).
    When the clock crosses into a new bar period, the finished bar is
    appended to _candles — without this, history stays frozen at whatever
    fetch_and_prime loaded at startup and every SMA/candle-based signal
    (SMA score, consolidation, bounce, retest bars) drifts off market
    within the hour.
    """
    if not ltp:
        return
    now = datetime.now(IST)
    if now.time() < _MARKET_OPEN:   # keep bar alignment: prime skips pre-open too
        return
    for tf in ALL_TFS:
        k = (instrument_key, tf)
        if k not in _candles:
            continue
        bar_start = _floor_to(now, _TF_MINUTES[tf])
        lb = _live_bar.get(k)
        if lb is None or lb[0] != bar_start:
            if lb is not None:
                # previous bar closed — commit it to history
                _candles[k].append((lb[1], lb[2], lb[3], lb[4]))
            lb = [bar_start, ltp, ltp, ltp, ltp]
            _live_bar[k] = lb
        else:
            lb[2] = max(lb[2], ltp)
            lb[3] = min(lb[3], ltp)
            lb[4] = ltp
        _smas[k] = _compute(list(_candles[k]) + [(lb[1], lb[2], lb[3], lb[4])])


def get_smas(instrument_key: str, tf: TimeFrame) -> dict[int, float | None]:
    return _smas.get((instrument_key, tf), {p: None for p in PERIODS})


_current_tf: TimeFrame = TimeFrame.M5


def set_current_tf(tf: TimeFrame) -> None:
    global _current_tf
    _current_tf = tf


def get_current_tf() -> TimeFrame:
    return _current_tf


def get_last_close(instrument_key: str, tf: TimeFrame = TimeFrame.M5) -> float:
    """Last bar close for given timeframe — fallback price when feed is offline."""
    candles = _candles.get((instrument_key, tf))
    if candles:
        return candles[-1][3]  # (O, H, L, C) → index 3
    return 0.0


def get_prev_close(instrument_key: str, tf: TimeFrame = TimeFrame.M5) -> float:
    """Second-to-last bar close — previous period's close for change calculation."""
    candles = _candles.get((instrument_key, tf))
    if candles and len(candles) >= 2:
        return candles[-2][3]
    return 0.0


def get_sma(instrument_key: str, tf: TimeFrame, period: int) -> float | None:
    return _smas.get((instrument_key, tf), {}).get(period)


def get_last_candles(instrument_key: str, tf: TimeFrame, n: int = 3) -> list[Candle]:
    """Return last n closed OHLCV bars (oldest first)."""
    q = _candles.get((instrument_key, tf))
    return list(q)[-n:] if q else []
