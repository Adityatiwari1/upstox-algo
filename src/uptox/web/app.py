"""
Read-only API for the trading dashboard. Separate process from the CLI app —
only reads logs/*.json, never mutates state, no live feed.

Run with:
    uvicorn uptox.web.app:app --host 0.0.0.0 --port 8080
or:
    uptox-web
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from uptox.web import data

app = FastAPI(title="Addy's Algo Trading — API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)


@app.get("/api/open-trade")
def api_open_trade():
    return data.get_open_trade() or {}


@app.get("/api/trades")
def api_trades(date: str | None = None):
    trades = data.get_all_trades()
    if date:
        trades = [t for t in trades if t.get("date") == date]
    return list(reversed(trades))


@app.get("/api/today")
def api_today():
    return data.get_today_stats()


@app.get("/api/daily")
def api_daily():
    return data.get_daily_breakdown()


@app.get("/api/capital")
def api_capital():
    return data.get_capital_info()


@app.get("/api/stats")
def api_stats():
    return data.get_stats()


def run() -> None:
    import uvicorn
    uvicorn.run("uptox.web.app:app", host="0.0.0.0", port=8080)
