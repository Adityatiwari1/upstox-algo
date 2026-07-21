"""Wires both quant strategies to the live 5-min bar-close feed — same
snapshot shape and cadence data_recorder.py already writes to
logs/market_data/*.jsonl, so live and backtest run identical code (see
backtest/quant_replay.py). One rolling history buffer feeds both
strategies; each strategy gets its own QuantEngine instance, own book
file, own independent paper/live mode."""
from __future__ import annotations

from pathlib import Path

from uptox.quant.directional_strategy import DirectionalStrategy
from uptox.quant.engine import QuantEngine
from uptox.quant.options_dynamics_strategy import OptionsDynamicsStrategy
from uptox.quant.quant_trading_mode import QuantTradingMode

_HISTORY_MAX = 500  # ~1.5 trading days of 5-min bars — plenty for the v1 rule windows


class QuantRunner:

    def __init__(self) -> None:
        self.history: list[dict] = []
        directional = DirectionalStrategy()
        options_dyn = OptionsDynamicsStrategy()
        self.engines: dict[str, QuantEngine] = {
            "directional": QuantEngine(
                directional,
                Path("logs/quant_directional_trades.jsonl"),
                QuantTradingMode("directional"),
            ),
            "options_dynamics": QuantEngine(
                options_dyn,
                Path("logs/quant_options_trades.jsonl"),
                QuantTradingMode("options_dynamics"),
            ),
        }

    def on_bar(self, row: dict | None) -> None:
        if row is None:
            return
        self.history.append(row)
        if len(self.history) > _HISTORY_MAX:
            self.history = self.history[-_HISTORY_MAX:]
        for engine in self.engines.values():
            engine.on_snapshot(row, self.history)


_runner: QuantRunner | None = None


def _get_runner() -> QuantRunner:
    global _runner
    if _runner is None:
        _runner = QuantRunner()
    return _runner
