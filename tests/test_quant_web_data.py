"""Web read layer for the quant dashboard — re-reads the JSONL book files
fresh on every call (same convention as web/data.py, since the web
process is separate from the live trading process)."""
import json
import tempfile
from pathlib import Path

from uptox.web import quant_data


def test_get_trades_unknown_strategy_returns_empty():
    quant_data._BOOK_PATHS["directional"] = Path("logs/does_not_exist_xyz.jsonl")
    assert quant_data.get_trades("directional") == []


def test_get_trades_reads_and_orders_newest_first():
    p = Path(tempfile.mkstemp(suffix=".jsonl")[1])
    with p.open("w", encoding="utf-8") as f:
        f.write(json.dumps({"event": "open", "id": "a1", "option": "CE", "entry_price": 100.0}) + "\n")
        f.write(json.dumps({"event": "close", "id": "a1", "exit_price": 110.0, "pnl_pts": 10.0, "pnl_rs": 6500.0}) + "\n")
        f.write(json.dumps({"event": "open", "id": "a2", "option": "PE", "entry_price": 90.0}) + "\n")
        f.write(json.dumps({"event": "close", "id": "a2", "exit_price": 80.0, "pnl_pts": 10.0, "pnl_rs": 6500.0}) + "\n")
    quant_data._BOOK_PATHS["directional"] = p
    trades = quant_data.get_trades("directional")
    assert len(trades) == 2
    assert trades[0]["id"] == "a2"  # newest first


def test_get_stats_matches_book_stats():
    p = Path(tempfile.mkstemp(suffix=".jsonl")[1])
    with p.open("w", encoding="utf-8") as f:
        f.write(json.dumps({"event": "open", "id": "a1", "option": "CE", "entry_price": 100.0}) + "\n")
        f.write(json.dumps({"event": "close", "id": "a1", "exit_price": 110.0, "pnl_pts": 10.0, "pnl_rs": 6500.0}) + "\n")
    quant_data._BOOK_PATHS["options_dynamics"] = p
    s = quant_data.get_stats("options_dynamics")
    assert s["total"] == 1
    assert s["total_pnl_rs"] == 6500.0
