"""Trades the option's own behavior — OI buildup, PCR level, IV move —
independent of a pure directional call. OI-direction reading reuses this
codebase's existing convention (option_chain_service.get_oi_signal):
CE-OI-up + PE-OI-down = bullish, and the reverse = bearish.

v1 thresholds are deliberately simple; sweep and validate against
backtest expectancy before tightening."""
from __future__ import annotations

from uptox.quant.strategy import Signal, Strategy


class OptionsDynamicsStrategy(Strategy):
    strategy_id = "options_dynamics"

    def generate_signal(self, history: list[dict]) -> Signal | None:
        if not history:
            return None
        cur = history[-1]
        ce_chg = cur.get("ce_oi_chg", 0)
        pe_chg = cur.get("pe_oi_chg", 0)
        ce_iv  = cur.get("ce_iv", 0.0)
        pe_iv  = cur.get("pe_iv", 0.0)
        pcr    = cur.get("pcr", 1.0)

        # Price-direction confirmation (long buildup vs short buildup) --
        # paused 2026-07-21: OI alone is ambiguous (every contract has a
        # buyer and a writer), so in theory pairing OI-rising with the same
        # option's own price direction should reject false positives where
        # OI rises on writer selling (short buildup) instead of buyer demand
        # (long buildup). Backtested WORSE on the 20-day sample despite the
        # sound theory: 56->29 trades, +Rs1710.12->-Rs314.45 expectancy/trade.
        # Watching OI-only live for now; revisit with more data before
        # re-enabling. To re-enable: uncomment below, add `and ce_ltp_up` /
        # `and pe_ltp_up` to the two conditions, and require len(history)>=2.
        # prev = history[-2] if len(history) >= 2 else cur
        # ce_ltp_up = cur.get("ce_ltp", 0.0) > prev.get("ce_ltp", 0.0)
        # pe_ltp_up = cur.get("pe_ltp", 0.0) > prev.get("pe_ltp", 0.0)

        if ce_chg > 0 and pe_chg < 0 and pcr < 1.0 and ce_iv >= pe_iv:
            return Signal(option="CE", reason="oi_bullish_pcr_low",
                          score=1.0, strategy_id=self.strategy_id)
        if ce_chg < 0 and pe_chg > 0 and pcr > 1.0 and pe_iv >= ce_iv:
            return Signal(option="PE", reason="oi_bearish_pcr_high",
                          score=-1.0, strategy_id=self.strategy_id)
        return None
