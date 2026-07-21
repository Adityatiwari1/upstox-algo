"""Read-only data access for the quant dashboard panels. Runs in the web
process, separate from the live trading process, so it re-reads the JSONL
book files fresh on every call — same convention as web/data.py."""
from __future__ import annotations

from pathlib import Path

from uptox.quant import book

_BOOK_PATHS: dict[str, Path] = {
    "directional":      Path("logs/quant_directional_trades.jsonl"),
    "options_dynamics": Path("logs/quant_options_trades.jsonl"),
}


def get_trades(strategy_id: str) -> list[dict]:
    path = _BOOK_PATHS.get(strategy_id)
    if not path:
        return []
    trades = book.closed_trades(path)
    return list(reversed(trades))


def get_stats(strategy_id: str) -> dict:
    path = _BOOK_PATHS.get(strategy_id)
    if not path:
        return book.stats([])
    return book.stats(book.closed_trades(path))
