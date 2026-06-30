"""
Read-only API for the trading dashboard. Separate process from the CLI app —
only reads logs/*.json, never mutates state, no live feed.

Run with:
    uvicorn uptox.web.app:app --host 0.0.0.0 --port 8080
or:
    uptox-web
"""
from __future__ import annotations

from typing import Dict, List

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from uptox.services import notification_settings as ns
from uptox.web import data
from uptox.web.schemas import (
    CapitalInfo,
    DailyBreakdownItem,
    NotificationEnabledBody,
    NotificationSetting,
    NotificationSubBody,
    TodayStats,
    Trade,
    TradeStats,
)

app = FastAPI(
    title="Addy's Algo Trading — API",
    description="Read-only API over the paper trading log. Reads logs/*.json fresh on every call.",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)


# ── Doc-only response shape (model + example). Routes still return plain
# dict/list, unchanged — `responses={200: {"model": ...}}` documents the real
# schema in Swagger/ReDoc without FastAPI coercing/validating the actual
# response through it (no behavior change, e.g. `{}` for "no open trade"
# still renders as `{}`, not an all-null object). ───────────────────────────

_EX_CLOSED_TRADE = {
    "id": "44529b52", "date": "2026-06-25", "time_entry": "13:57:45",
    "entry_ts": 1782376065.12, "time_exit": "14:13:24", "exit_ts": 1782376804.31,
    "strike": 24150, "option": "CE", "expiry": "2026-06-30", "option_key": "NSE_FO|79734",
    "entry_price": 125.2, "exit_price": 106.7, "lots": 2, "qty": 130, "lot_size": 65,
    "invested": 16282.0,
    "sl": 107.2, "target": 161.2, "delta": 0.575,
    "spot_entry": 24174.7, "spot_current": 24162.3, "spot_peak": 24181.0,
    "spot_exit": 24162.3, "spot_move_pts": -12.4, "spot_directional_pts": -12.4,
    "spot_peak_move_pts": 6.3,
    "pnl_pts": -18.5, "pnl_rs": -2405.0, "outcome": "SL_HIT", "exit_reason": "trail_sl",
    "entry_reason": "sustained | SMA+2 Flow+3 | Nifty@24175 | CE@24150 | NORMAL ATR=28 | risk=₹2346 (3.5%)",
    "score": 5, "sma_score": 2, "flow_score": 3, "atr_at_entry": 27.8, "expiry_day": False,
    "peak_price": 132.2, "max_pnl_pts": 7.0, "max_pnl_rs": 910.0,
}

_EX_OPEN_TRADE = {
    "id": "9f3a21bc", "date": "2026-06-26", "time_entry": "10:42:10",
    "entry_ts": 1782460930.0, "time_exit": None, "strike": 24200,
    "option": "PE", "expiry": "2026-06-30", "option_key": "NSE_FO|79801",
    "entry_price": 98.5, "exit_price": None, "lots": 1, "qty": 65, "lot_size": 65,
    "invested": 6402.5,
    "sl": 88.0, "target": 119.5, "delta": -0.48,
    "spot_entry": 24198.3, "spot_current": 24186.1, "spot_peak": 24180.4,
    "current_price": 104.2, "pnl_pts": 5.7, "pnl_rs": 370.0, "outcome": "OPEN",
    "exit_reason": None, "entry_reason": "retest | SMA-2 Flow-1 | Nifty@24198 | PE@24200",
    "score": -3, "sma_score": -2, "flow_score": -1, "atr_at_entry": 26.1, "expiry_day": False,
}

_EX_STATS = {
    "total": 16, "wins": 11, "losses": 5, "win_rate": 68.8,
    "total_pnl_pts": 152.6, "total_pnl_rs": 13901.0,
    "avg_win": 17.8, "avg_loss": -8.7, "best": 58.0, "worst": -22.0,
    "expectancy": 9.4, "avg_rr": 2.04,
}

_EX_TODAY = {**_EX_STATS, "total": 0, "wins": 0, "losses": 0, "win_rate": 0.0,
             "total_pnl_pts": 0.0, "total_pnl_rs": 0.0, "date": "2026-06-26", "pnl_pct_today": 0.0}

_EX_DAILY = [{**_EX_STATS, "total": 5, "wins": 3, "losses": 2, "win_rate": 60.0,
              "total_pnl_pts": 8.2, "total_pnl_rs": 1066.0, "date": "2026-06-25"}]

_EX_CAPITAL = {
    "base": 50000.0, "current": 63901.0, "realised_lifetime": 13901.0,
    "daily_loss_pct": 3.0, "max_trades_per_day": 5, "max_consec_losses": 3,
}

_EX_NOTIFICATIONS = {
    "trade_open": {"enabled": True},
    "trade_update": {"enabled": True, "interval_min": 15},
    "approaching_limit": {"enabled": True, "threshold_pct": 75.0},
    "weekly_report": {"enabled": True, "weekday": "saturday", "time": "11:00"},
}

_EX_NOTIFICATION_SINGLE = {"enabled": True, "interval_min": 15}


def _doc(model, example):
    """200-response doc: real Pydantic schema for Swagger + a realistic example."""
    return {200: {"model": model, "content": {"application/json": {"example": example}}}}


@app.get("/api/open-trade", summary="Current open trade", tags=["trades"],
          responses=_doc(Trade, _EX_OPEN_TRADE))
def api_open_trade():
    """Returns the currently open paper trade, or `{}` if none is open."""
    return data.get_open_trade() or {}


@app.get("/api/trades", summary="Closed trade history", tags=["trades"],
          responses=_doc(List[Trade], [_EX_CLOSED_TRADE]))
def api_trades(
    frm: str | None = None,
    to:  str | None = None,
):
    """
    All closed trades, newest first.
    - `?frm=YYYY-MM-DD`        — from date (inclusive)
    - `?to=YYYY-MM-DD`         — to date (inclusive)
    - `?frm=YYYY-MM-DD&to=...` — date range
    """
    trades = data.get_all_trades()
    if frm:
        trades = [t for t in trades if t.get("date", "") >= frm]
    if to:
        trades = [t for t in trades if t.get("date", "") <= to]
    return list(reversed(trades))


@app.get("/api/today", summary="Today's stats", tags=["analysis"],
          responses=_doc(TodayStats, _EX_TODAY))
def api_today():
    """Today's trade stats plus today's P&L as % of current capital."""
    return data.get_today_stats()


@app.get("/api/daily", summary="Per-day breakdown", tags=["analysis"],
          responses=_doc(List[DailyBreakdownItem], _EX_DAILY))
def api_daily():
    """One stats row per trading day, most recent day first."""
    return data.get_daily_breakdown()


@app.get("/api/capital", summary="Capital snapshot", tags=["analysis"],
          responses=_doc(CapitalInfo, _EX_CAPITAL))
def api_capital():
    """Base capital, current capital, and lifetime realised P&L."""
    return data.get_capital_info()


@app.get("/api/stats", summary="Lifetime stats", tags=["analysis"],
          responses=_doc(TradeStats, _EX_STATS))
def api_stats():
    """Aggregate stats (win rate, expectancy, avg RR, etc.) across all closed trades."""
    return data.get_stats()


# ── Notification settings ───────────────────────────────────────────────────
# Writes go through notification_settings.py (same module the CLI uses) so the
# on-disk file (logs/notification_settings.json) stays the single source of
# truth. The CLI's Settings screen polls that file every few seconds and picks
# up changes made here without a restart.

@app.get("/api/notifications", summary="All notification settings", tags=["notifications"],
          responses=_doc(Dict[str, NotificationSetting], _EX_NOTIFICATIONS))
def api_get_notifications():
    """Every notification type with its current enabled flag and sub-settings."""
    return ns.get_all()


@app.post("/api/notifications/{key}/enabled", summary="Toggle a notification type", tags=["notifications"],
           responses=_doc(NotificationSetting, _EX_NOTIFICATION_SINGLE))
def api_set_notification_enabled(key: str, body: NotificationEnabledBody):
    """Enable or disable one notification type (e.g. `trade_open`, `daily_report`)."""
    if key not in ns.get_all():
        raise HTTPException(status_code=404, detail=f"Unknown notification key: {key}")
    ns.set_enabled(key, body.enabled)
    ns.save()
    return ns.get_all()[key]


@app.post("/api/notifications/{key}/sub", summary="Set a notification sub-setting", tags=["notifications"],
           responses=_doc(NotificationSetting, _EX_NOTIFICATION_SINGLE))
def api_set_notification_sub(key: str, body: NotificationSubBody):
    """
    Set a tunable sub-setting. Numeric settings (e.g. `interval_min`) are clamped
    to allowed bounds. For the weekly report schedule, use:
    `{"sub_key": "weekday", "value": "saturday"}` and `{"sub_key": "time", "value": "11:00"}`
    (24h format). Default is Saturday 11:00 IST — runs independent of the market feed.
    """
    if key not in ns.get_all():
        raise HTTPException(status_code=404, detail=f"Unknown notification key: {key}")
    error = ns.validate_sub(key, body.sub_key, body.value)
    if error:
        raise HTTPException(status_code=422, detail=error)
    ns.set_sub(key, body.sub_key, body.value)
    ns.save()
    return ns.get_all()[key]


def run() -> None:
    import uvicorn
    uvicorn.run("uptox.web.app:app", host="0.0.0.0", port=8080)
