"""
NSE market calendar — trading day + time-window helpers.

Uses `exchange_calendars` (XNSE) for holiday accuracy.
Falls back to weekday + hardcoded list if package unavailable.

Trading windows (IST):
  09:00–09:30  Pre-market analysis (no order execution)
  09:30–15:25  Live trading window
  15:25–15:35  Feed winds down, all positions force-closed at 15:25
"""
from __future__ import annotations
from datetime import date, datetime, time as dt_time
from zoneinfo import ZoneInfo

_IST = ZoneInfo("Asia/Kolkata")

PREMARKET_START = dt_time(9,  0)
TRADE_START     = dt_time(9, 30)
TRADE_END       = dt_time(15, 25)
FEED_END        = dt_time(15, 35)


# ── Holiday lookup ────────────────────────────────────────────────────────────

def is_trading_day(check_date: date | None = None) -> bool:
    """Return True if `check_date` is an NSE trading session."""
    d = check_date or datetime.now(_IST).date()
    try:
        import exchange_calendars as xcals
        cal = xcals.get_calendar("XNSE")
        return bool(cal.is_session(d.isoformat()))
    except Exception:
        return _fallback_is_trading_day(d)


def next_trading_day(from_date: date | None = None) -> date:
    """Return the next trading day on or after `from_date`."""
    d = from_date or datetime.now(_IST).date()
    try:
        import exchange_calendars as xcals
        import pandas as pd
        cal = xcals.get_calendar("XNSE")
        # next_open returns a Timestamp; .date() gives us the session date
        return cal.next_open(pd.Timestamp(d)).date()
    except Exception:
        import datetime as _dt
        while not _fallback_is_trading_day(d):
            d = d + _dt.timedelta(days=1)
        return d


# ── Time-window helpers ───────────────────────────────────────────────────────

def is_feed_active() -> bool:
    """True if the feed should be live right now (09:00–15:35 on a trading day)."""
    now = datetime.now(_IST)
    if not is_trading_day(now.date()):
        return False
    return PREMARKET_START <= now.time() <= FEED_END


def is_trade_window() -> bool:
    """True if order execution is allowed (09:30–15:25 IST on a trading day)."""
    now = datetime.now(_IST)
    if not is_trading_day(now.date()):
        return False
    return TRADE_START <= now.time() <= TRADE_END


def is_premarket() -> bool:
    """True during pre-market analysis window (09:00–09:29 IST)."""
    now = datetime.now(_IST)
    if not is_trading_day(now.date()):
        return False
    return PREMARKET_START <= now.time() < TRADE_START


def market_status() -> str:
    """Short human-readable status string for the dashboard."""
    now   = datetime.now(_IST)
    today = now.date()
    if not is_trading_day(today):
        nd = next_trading_day(today)
        return f"Market closed — next trading day {nd.strftime('%a %d %b')}"
    t = now.time()
    if t < PREMARKET_START:
        return "Market opens at 09:00"
    if t < TRADE_START:
        return "Pre-market (analysis) — trading starts 09:30"
    if t <= TRADE_END:
        return "Market open"
    if t <= FEED_END:
        return "Market closed — winding down"
    return "Market closed for today"


# ── High-volatility event calendar ───────────────────────────────────────────
# On these days algo requires score ≥ 4 (not default 2) before entering.
# Update annually. RBI decision day = last day of 3-day MPC meeting.
_EVENT_DAYS: dict[date, str] = {
    # ── Budget ────────────────────────────────────────────────────────────────
    date(2026, 2,  1): "Union Budget 2026-27",
    # ── RBI MPC decision days (2026) ─────────────────────────────────────────
    date(2026, 2,  7): "RBI MPC — rate decision",
    date(2026, 4,  9): "RBI MPC — rate decision",
    date(2026, 6,  6): "RBI MPC — rate decision",
    date(2026, 8,  8): "RBI MPC — rate decision",
    date(2026, 10, 9): "RBI MPC — rate decision",
    date(2026, 12, 5): "RBI MPC — rate decision",
    # ── US Fed FOMC (spillover volatility on Indian markets) ─────────────────
    date(2026, 1, 29): "US Fed FOMC",
    date(2026, 3, 19): "US Fed FOMC",
    date(2026, 5,  7): "US Fed FOMC",
    date(2026, 6, 18): "US Fed FOMC",
    date(2026, 7, 30): "US Fed FOMC",
    date(2026, 9, 17): "US Fed FOMC",
    date(2026, 11, 5): "US Fed FOMC",
    date(2026, 12,17): "US Fed FOMC",
    # ── Quarterly results season (Nifty 50 heavyweights) — add specific dates
    # when known. Entire results season (2nd-3rd week of Apr/Jul/Oct/Jan)
    # is elevated vol but tradeable with high confidence.
}

_EVENT_MIN_SCORE = 4   # minimum |total score| to trade on event days


def get_event_today(check_date: date | None = None) -> str | None:
    """Return event name if today is a high-volatility event day, else None."""
    d = check_date or datetime.now(_IST).date()
    return _EVENT_DAYS.get(d)


def event_min_score() -> int:
    """Score threshold to use today (higher on event days)."""
    return _EVENT_MIN_SCORE if get_event_today() else 2


def is_last_trading_day_of_week(check_date: date | None = None) -> bool:
    """True if no more trading days remain in the current ISO week (Mon-Sun)."""
    import datetime as _dt
    d = check_date or datetime.now(_IST).date()
    nxt = next_trading_day(d + _dt.timedelta(days=1))
    return nxt.isocalendar()[1] != d.isocalendar()[1] or nxt.year != d.year


def is_last_trading_day_of_month(check_date: date | None = None) -> bool:
    """True if no more trading days remain in the current calendar month."""
    import datetime as _dt
    d = check_date or datetime.now(_IST).date()
    nxt = next_trading_day(d + _dt.timedelta(days=1))
    return nxt.month != d.month or nxt.year != d.year


# ── Fallback (no exchange_calendars) ─────────────────────────────────────────

_NSE_HOLIDAYS: set[date] = {
    # 2025
    date(2025, 1, 26), date(2025, 2, 26), date(2025, 3, 14),
    date(2025, 3, 31), date(2025, 4, 10), date(2025, 4, 14),
    date(2025, 4, 18), date(2025, 5,  1), date(2025, 8, 15),
    date(2025, 8, 27), date(2025, 10, 2), date(2025, 10, 24),
    date(2025, 11, 5), date(2025, 12, 25),
    # 2026
    date(2026, 1, 26), date(2026, 2, 26), date(2026, 3, 25),
    date(2026, 4,  2), date(2026, 4,  3), date(2026, 4, 14),
    date(2026, 5,  1), date(2026, 8, 15), date(2026, 10, 2),
    date(2026, 10, 22), date(2026, 11, 11), date(2026, 11, 12),
    date(2026, 11, 25), date(2026, 12, 25),
}


def _fallback_is_trading_day(d: date) -> bool:
    if d.weekday() >= 5:
        return False
    return d not in _NSE_HOLIDAYS
