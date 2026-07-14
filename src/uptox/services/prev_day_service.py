"""
Previous trading day OHLC for Nifty — static intraday reference levels.

Fetched once at startup via the Upstox historical-candle REST API (daily
interval). Unlike today's high/low these never move during the session, which
makes them clean S/R inputs:
  - PDH / PDL   → most-watched institutional intraday levels
  - Pivot PP/R1/S1 → classic floor-trader levels derived from H, L, C
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from urllib.parse import quote

import httpx

_log = logging.getLogger(__name__)

NIFTY_KEY = "NSE_INDEX|Nifty 50"
_URL_TMPL = "https://api.upstox.com/v2/historical-candle/{key}/day/{to}/{frm}"

_ohlc: dict | None = None   # {date, open, high, low, close}
_primed_on: str | None = None   # the session date this cache is valid FOR
_refreshing: bool = False


async def fetch_and_prime(access_token: str) -> None:
    """Fetch the last week of daily candles, cache the most recent completed day."""
    global _ohlc, _primed_on
    today = date.today()
    _primed_on = today.isoformat()
    url = _URL_TMPL.format(
        key=quote(NIFTY_KEY, safe=""),
        to=today.isoformat(),
        frm=(today - timedelta(days=7)).isoformat(),
    )
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(url, headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/json",
            })
        r.raise_for_status()
        candles = r.json().get("data", {}).get("candles", [])
        # Candles come newest-first; ts like "2026-07-01T00:00:00+05:30"
        for cd in candles:
            d = str(cd[0])[:10]
            if d < today.isoformat():
                _ohlc = {
                    "date":  d,
                    "open":  float(cd[1]),
                    "high":  float(cd[2]),
                    "low":   float(cd[3]),
                    "close": float(cd[4]),
                }
                _log.info(
                    "Prev-day OHLC (%s): O=%.1f H=%.1f L=%.1f C=%.1f",
                    d, _ohlc["open"], _ohlc["high"], _ohlc["low"], _ohlc["close"],
                )
                return
        _log.warning("prev_day: no completed daily candle in response")
    except Exception as e:
        _log.exception("prev_day fetch error: %s", e)


def _refresh_async() -> None:
    """Kick off a background re-fetch; callers keep getting None until it lands."""
    global _refreshing
    if _refreshing:
        return
    _refreshing = True
    _log.warning("prev_day OHLC stale (primed on %s) — re-fetching for today", _primed_on)
    import asyncio
    from uptox.config.settings import settings

    async def _run() -> None:
        global _refreshing
        try:
            await fetch_and_prime(settings.access_token)
        finally:
            _refreshing = False

    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_run())
    except RuntimeError:
        asyncio.run(_run())


def get_ohlc() -> dict | None:
    """Previous trading day's OHLC, or None if not primed.

    Date-guarded: the cache is only valid on the session day it was fetched.
    The app runs overnight in tmux, so a startup-only fetch served FRIDAY's
    candle all of Tuesday 2026-07-14 — pdl_reclaim armed off Jul-10's low
    (24,123) instead of Jul-13's (24,023). Wrong-day levels are worse than
    none: return None and re-fetch in the background until fresh."""
    if _ohlc and _primed_on != date.today().isoformat():
        _refresh_async()
        return None
    return _ohlc


def get_pivots() -> dict | None:
    """Classic pivot levels from prev-day H/L/C, or None if not primed/fresh."""
    if not get_ohlc():
        return None
    h, l, c = _ohlc["high"], _ohlc["low"], _ohlc["close"]
    pp = (h + l + c) / 3
    return {
        "pp": round(pp, 1),
        "r1": round(2 * pp - l, 1),
        "s1": round(2 * pp - h, 1),
        "r2": round(pp + (h - l), 1),
        "s2": round(pp - (h - l), 1),
    }
