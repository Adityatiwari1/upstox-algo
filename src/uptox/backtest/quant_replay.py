"""Replays historical logs/market_data/*.jsonl rows through the exact same
Strategy + QuantEngine code used live, always in forced-paper mode — so a
backtest number is a genuine prediction of live behavior instead of an
approximation. This supersedes backtest/engine.py's premium approximation
(delta x spot move) for quant strategies: real ce_ltp/pe_ltp/oi/iv/pcr are
now recorded per bar and used directly.
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from uptox.quant import book
from uptox.quant.engine import QuantEngine
from uptox.quant.quant_trading_mode import QuantTradingMode
from uptox.quant.strategy import Strategy


def _read_rows(path: Path) -> list[dict]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def replay(strategy: Strategy, jsonl_paths: list[Path], capital: float = 20_000.0) -> dict:
    """Runs `strategy` through a fresh QuantEngine over every row of every
    path in `jsonl_paths`, in the order given (caller sorts paths
    chronologically — typically sorted(Path("logs/market_data").glob("*.jsonl"))).
    Always forced to paper mode, regardless of that strategy_id's real
    promoted mode, so a backtest run can never place a live order."""
    forced_paper_mode = QuantTradingMode(f"replay_{strategy.strategy_id}")
    forced_paper_mode._file = Path(tempfile.mkstemp(suffix=".json")[1])
    forced_paper_mode._mode = "paper"

    book_path = Path(tempfile.mkstemp(suffix=".jsonl")[1])
    engine = QuantEngine(strategy, book_path, forced_paper_mode, capital=capital)

    history: list[dict] = []
    for path in jsonl_paths:
        for row in _read_rows(path):
            history.append(row)
            engine.on_snapshot(row, history)

    trades = book.closed_trades(book_path)
    result = book.stats(trades)
    result["trades"] = trades
    return result
