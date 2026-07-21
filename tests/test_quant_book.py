"""JSONL open/close event pairing and stats — same shape convention as
shadow_book.py, parameterized by path so multiple books share this code."""
import json
import tempfile
from pathlib import Path

from uptox.quant import book


def _tmp_path() -> Path:
    return Path(tempfile.mkstemp(suffix=".jsonl")[1])


def test_append_and_read_events():
    p = _tmp_path()
    book.append_event(p, {"event": "open", "id": "a1", "option": "CE"})
    book.append_event(p, {"event": "close", "id": "a1", "pnl_pts": 5.0})
    rows = book.read_events(p)
    assert len(rows) == 2
    assert rows[0]["event"] == "open"
    assert rows[1]["event"] == "close"


def test_read_events_missing_file_returns_empty():
    assert book.read_events(Path("logs/does_not_exist_xyz.jsonl")) == []


def test_read_events_skips_corrupt_lines():
    p = _tmp_path()
    p.write_text('{"event": "open", "id": "a1"}\nNOT JSON\n{"event": "close", "id": "a1"}\n',
                 encoding="utf-8")
    rows = book.read_events(p)
    assert len(rows) == 2


def test_closed_trades_pairs_open_and_close():
    p = _tmp_path()
    book.append_event(p, {"event": "open", "id": "a1", "option": "CE", "entry_price": 100.0})
    book.append_event(p, {"event": "close", "id": "a1", "exit_price": 110.0,
                           "pnl_pts": 10.0, "pnl_rs": 6500.0})
    book.append_event(p, {"event": "open", "id": "a2", "option": "PE", "entry_price": 50.0})
    trades = book.closed_trades(p)
    assert len(trades) == 1  # a2 never closed
    assert trades[0]["option"] == "CE"
    assert trades[0]["exit_price"] == 110.0
    assert trades[0]["pnl_pts"] == 10.0


def test_stats_on_empty_trades():
    s = book.stats([])
    assert s["total"] == 0
    assert s["win_rate"] == 0.0
    assert s["expectancy_rs"] == 0.0


def test_stats_computes_win_rate_and_expectancy():
    trades = [
        {"pnl_pts": 10.0, "pnl_rs": 6500.0},
        {"pnl_pts": -5.0, "pnl_rs": -3250.0},
        {"pnl_pts": 8.0,  "pnl_rs": 5200.0},
    ]
    s = book.stats(trades)
    assert s["total"] == 3
    assert s["wins"] == 2
    assert s["losses"] == 1
    assert round(s["win_rate"], 1) == 66.7
    assert s["total_pnl_pts"] == 13.0
    assert s["total_pnl_rs"] == 8450.0
    assert round(s["expectancy_pts"], 2) == round(13.0 / 3, 2)
