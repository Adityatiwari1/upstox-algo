"""Append-only JSONL open/close event log, shared by every quant book —
same shape convention as shadow_book.py, parameterized by path so the
directional and options-dynamics books use identical code."""
from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)


def append_event(path: Path, row: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError as e:
        log.warning("quant.book: could not write %s: %s", path, e)


def read_events(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as e:
        log.warning("quant.book: could not read %s: %s", path, e)
        return []
    rows = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def closed_trades(path: Path) -> list[dict]:
    """Pair open/close events by id into flat trade dicts. An open without
    a matching close (still-open position, or app restarted mid-trade) is
    dropped — callers wanting the open position use a different lookup."""
    opens: dict[str, dict] = {}
    trades: list[dict] = []
    for row in read_events(path):
        rid = row.get("id")
        if row.get("event") == "open" and rid:
            opens[rid] = row
        elif row.get("event") == "close" and rid:
            o = opens.pop(rid, None)
            if o:
                trades.append({**o, **row})
    return trades


def stats(trades: list[dict]) -> dict:
    total = len(trades)
    if total == 0:
        return {
            "total": 0, "wins": 0, "losses": 0, "win_rate": 0.0,
            "total_pnl_pts": 0.0, "total_pnl_rs": 0.0,
            "expectancy_pts": 0.0, "expectancy_rs": 0.0,
        }
    wins   = sum(1 for t in trades if t.get("pnl_pts", 0.0) > 0)
    losses = total - wins
    total_pts = sum(t.get("pnl_pts", 0.0) for t in trades)
    total_rs  = sum(t.get("pnl_rs", 0.0) for t in trades)
    return {
        "total": total, "wins": wins, "losses": losses,
        "win_rate": round(wins / total * 100, 2),
        "total_pnl_pts": round(total_pts, 2),
        "total_pnl_rs": round(total_rs, 2),
        "expectancy_pts": round(total_pts / total, 2),
        "expectancy_rs": round(total_rs / total, 2),
    }
