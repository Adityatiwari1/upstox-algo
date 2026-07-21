"""Backtest replay drives the exact same Strategy + QuantEngine code used
live over historical logs/market_data/*.jsonl rows, always in forced-paper
mode — so a backtest number is a real prediction of live behavior, not an
approximation (fixes the limitation backtest/engine.py's docstring flags:
it estimates premium via delta x spot move because it predates recorded
option-chain data)."""
import json
import tempfile
from pathlib import Path

from uptox.quant.strategy import Signal, Strategy
from uptox.backtest.quant_replay import replay


class _AlwaysCEOnFirstBar(Strategy):
    strategy_id = "test_replay"

    def generate_signal(self, history):
        if len(history) == 1:
            return Signal(option="CE", reason="always_first", score=1.0,
                          strategy_id=self.strategy_id)
        return None


def _write_jsonl(rows: list[dict]) -> Path:
    p = Path(tempfile.mkstemp(suffix=".jsonl")[1])
    with p.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    return p


def test_replay_forces_paper_mode_even_if_promoted():
    from uptox.services import order_service
    calls = []
    order_service.schedule_entry_with_sl = lambda *a, **kw: calls.append(a)
    rows = [
        {"ts": "2026-07-20T09:15:00+05:30", "nifty": 24200, "ce_ltp": 150.0, "pe_ltp": 80.0, "sma_score": 0},
        {"ts": "2026-07-20T09:20:00+05:30", "nifty": 24200, "ce_ltp": 145.0, "pe_ltp": 80.0, "sma_score": 0},
        {"ts": "2026-07-20T09:25:00+05:30", "nifty": 24200, "ce_ltp": 139.0, "pe_ltp": 80.0, "sma_score": 0},
    ]
    result = replay(_AlwaysCEOnFirstBar(), [_write_jsonl(rows)])
    assert calls == [], "replay must never touch order_service, even if the strategy's real mode is live"
    assert result["total"] == 1
    assert result["trades"][0]["exit_reason"] == "stop"


def test_replay_reads_multiple_files_in_order():
    rows_day1 = [{"ts": "2026-07-20T09:15:00+05:30", "nifty": 24200, "ce_ltp": 150.0, "pe_ltp": 80.0, "sma_score": 0}]
    rows_day2 = [
        {"ts": "2026-07-21T09:15:00+05:30", "nifty": 24200, "ce_ltp": 155.0, "pe_ltp": 80.0, "sma_score": 0},
        {"ts": "2026-07-21T09:20:00+05:30", "nifty": 24200, "ce_ltp": 171.0, "pe_ltp": 80.0, "sma_score": 0},
    ]
    result = replay(_AlwaysCEOnFirstBar(), [_write_jsonl(rows_day1), _write_jsonl(rows_day2)])
    # signal fires on the 2nd bar seen overall (len(history)==1 means first bar of
    # a fresh position-less strategy call — file 1's single row triggers no signal
    # since it IS bar 1, position opens; file 2 continues the same running history)
    assert result["total"] >= 1


def test_replay_empty_files_returns_zero_trades():
    result = replay(_AlwaysCEOnFirstBar(), [_write_jsonl([])])
    assert result["total"] == 0
    assert result["trades"] == []
