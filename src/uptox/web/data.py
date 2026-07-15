"""
Read-only data access for the web dashboard.

Runs as a separate process from the CLI app — re-reads logs/*.json fresh on
every call instead of importing the live service modules (which cache state
in memory at import time and would go stale across processes).
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

_IST = ZoneInfo("Asia/Kolkata")

_TRADES_FILE     = Path("logs/trades.json")
_OPEN_TRADE_FILE = Path("logs/open_trade.json")
_CAPITAL_FILE    = Path("logs/capital.json")


def _read_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def get_all_trades() -> list[dict]:
    return _read_json(_TRADES_FILE, [])


def get_open_trade() -> dict | None:
    data = _read_json(_OPEN_TRADE_FILE, None)
    if data and data.get("outcome") == "OPEN":
        return data
    return None


def get_capital_info() -> dict:
    cap = _read_json(_CAPITAL_FILE, {})
    base = float(cap.get("base", 10_000.0))
    # Net of real broker charges where priced — matches capital_service.sync_from_history
    realised = sum(
        float(t["pnl_rs_net"]) if t.get("pnl_rs_net") is not None else float(t.get("pnl_rs", 0.0))
        for t in get_all_trades()
    )
    return {
        "base":                round(base, 2),
        "current":             round(base + realised, 2),
        "realised_lifetime":   round(realised, 2),
        "daily_loss_pct":      cap.get("daily_loss_pct"),
        "max_trades_per_day":  cap.get("max_trades_per_day"),
        "max_consec_losses":   cap.get("max_consec_losses"),
    }


def get_stats(trades: list[dict] | None = None) -> dict:
    closed = trades if trades is not None else get_all_trades()
    if not closed:
        return {
            "total": 0, "wins": 0, "losses": 0, "win_rate": 0.0,
            "total_pnl_pts": 0.0, "total_pnl_rs": 0.0,
            "total_cost_rs": 0.0, "total_pnl_rs_net": 0.0,
            "avg_win": 0.0, "avg_loss": 0.0,
            "best": 0.0, "worst": 0.0, "expectancy": 0.0, "avg_rr": 0.0,
        }
    wins   = [t for t in closed if t.get("pnl_pts", 0) > 0]
    losses = [t for t in closed if t.get("pnl_pts", 0) <= 0]
    avg_w  = sum(t["pnl_pts"] for t in wins)   / len(wins)   if wins   else 0.0
    avg_l  = abs(sum(t["pnl_pts"] for t in losses) / len(losses)) if losses else 0.0
    wr     = len(wins) / len(closed)
    expect = wr * avg_w - (1 - wr) * avg_l
    avg_rr = round(avg_w / avg_l, 2) if avg_l else 0.0
    total_rs   = sum(t["pnl_rs"] for t in closed)
    total_cost = sum(t.get("cost_rs") or 0.0 for t in closed)
    return {
        "total":         len(closed),
        "wins":          len(wins),
        "losses":        len(losses),
        "win_rate":      round(wr * 100, 1),
        "total_pnl_pts": round(sum(t["pnl_pts"] for t in closed), 1),
        "total_pnl_rs":  round(total_rs, 0),
        "total_cost_rs":    round(total_cost, 0),
        "total_pnl_rs_net": round(total_rs - total_cost, 0),
        "avg_win":       round(avg_w, 1),
        "avg_loss":      round(-avg_l, 1),
        "best":          round(max((t["pnl_pts"] for t in closed), default=0.0), 1),
        "worst":         round(min((t["pnl_pts"] for t in closed), default=0.0), 1),
        "expectancy":    round(expect, 1),
        "avg_rr":        avg_rr,
    }


def get_today_stats() -> dict:
    today = datetime.now(_IST).strftime("%Y-%m-%d")
    today_trades = [t for t in get_all_trades() if t.get("date") == today]
    s   = get_stats(today_trades)
    cap = get_capital_info()
    pnl_pct = round(s["total_pnl_rs_net"] / cap["current"] * 100, 2) if cap["current"] else 0.0
    return {**s, "date": today, "pnl_pct_today": pnl_pct}


def get_daily_breakdown() -> list[dict]:
    """One row per trading day: date + that day's stats, most recent first."""
    by_date: dict[str, list[dict]] = {}
    for t in get_all_trades():
        by_date.setdefault(t.get("date", "unknown"), []).append(t)
    return [
        {"date": date, **get_stats(by_date[date])}
        for date in sorted(by_date.keys(), reverse=True)
    ]
