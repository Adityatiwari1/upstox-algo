"""Predicts NIFTY spot direction: SMA-score at its extreme (+2 or -2)
confirmed by the last bar's momentum in the same direction, expressed by
buying ATM CE/PE. v1 thresholds are deliberately simple (score extreme +
1-bar momentum) — sweep and validate against backtest expectancy before
tightening; ML is out of scope until more data accumulates."""
from __future__ import annotations

from uptox.quant.strategy import Signal, Strategy


class DirectionalStrategy(Strategy):
    strategy_id = "directional"

    def generate_signal(self, history: list[dict]) -> Signal | None:
        if len(history) < 2:
            return None
        cur, prev = history[-1], history[-2]
        score = cur.get("sma_score", 0)
        moved_up   = cur.get("nifty", 0) > prev.get("nifty", 0)
        moved_down = cur.get("nifty", 0) < prev.get("nifty", 0)

        if score == 2 and moved_up:
            return Signal(option="CE", reason="sma_score+2_momentum_up",
                          score=float(score), strategy_id=self.strategy_id)
        if score == -2 and moved_down:
            return Signal(option="PE", reason="sma_score-2_momentum_down",
                          score=float(score), strategy_id=self.strategy_id)
        return None
