"""
Backtesting engine.

Replays historical Nifty 5-min candles through the same signal logic
used in live trading. Option price is approximated (delta × Nifty move)
since historical option tick data is not available.

Limitations vs live:
  - flow_score = 0 (no historical delta/OI data)
  - Option premium estimated, not actual
  - No slippage model (add ~3pt to every entry in your head)
  - Breakout-retest simplified: enters on signal bar close

Usage:
    from uptox.backtest.engine import BacktestEngine
    engine = BacktestEngine(capital=30000)
    result = engine.run(candles, from_idx=0)
    result.print_summary()
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Literal

log = logging.getLogger(__name__)

# Candle tuple: (open, high, low, close)  — same as sma_service
Candle = tuple[float, float, float, float]

PERIODS    = (10, 20, 50, 100, 200)
ATM_DELTA  = 0.45    # approximate ATM option delta
ATM_PCT    = 0.004   # ATM weekly premium ≈ 0.4% of spot
IV_ASSUMED = 0.15    # 15% IV assumed for premium sizing


# ── Result dataclasses ────────────────────────────────────────────────────────

@dataclass
class BtTrade:
    idx:          int
    date_str:     str
    option:       Literal["CE", "PE"]
    spot_entry:   float
    opt_entry:    float
    opt_sl:       float
    opt_target:   float
    spot_exit:    float
    opt_exit:     float
    exit_reason:  str
    pnl_pts:      float   # option points
    pnl_rs:       float   # ₹
    lots:         int
    peak_pts:     float   # max profit reached intra-trade


@dataclass
class BacktestResult:
    trades:       list[BtTrade] = field(default_factory=list)
    final_capital: float = 0.0
    start_capital: float = 0.0

    @property
    def wins(self):   return [t for t in self.trades if t.pnl_pts > 0]
    @property
    def losses(self): return [t for t in self.trades if t.pnl_pts <= 0]
    @property
    def win_rate(self): return len(self.wins) / len(self.trades) * 100 if self.trades else 0
    @property
    def total_pnl_rs(self): return sum(t.pnl_rs for t in self.trades)
    @property
    def max_drawdown(self):
        peak = self.start_capital
        dd   = 0.0
        cap  = self.start_capital
        for t in self.trades:
            cap += t.pnl_rs
            peak = max(peak, cap)
            dd   = max(dd, peak - cap)
        return dd

    def print_summary(self) -> None:
        sep = "─" * 52
        print(sep)
        print(f"  BACKTEST RESULTS")
        print(sep)
        print(f"  Trades      : {len(self.trades)}  ({len(self.wins)}W / {len(self.losses)}L)")
        print(f"  Win rate    : {self.win_rate:.1f}%")
        if self.trades:
            avg_w = sum(t.pnl_rs for t in self.wins)   / max(len(self.wins),   1)
            avg_l = sum(t.pnl_rs for t in self.losses) / max(len(self.losses), 1)
            print(f"  Avg win     : ₹{avg_w:,.0f}")
            print(f"  Avg loss    : ₹{avg_l:,.0f}")
            rr = abs(avg_w / avg_l) if avg_l else 0
            print(f"  R:R ratio   : {rr:.2f}")
        print(f"  Total P&L   : ₹{self.total_pnl_rs:+,.0f}")
        peak = max((self.start_capital + sum(t.pnl_rs for t in self.trades[:i+1]))
                   for i in range(len(self.trades))) if self.trades else self.start_capital
        peak = max(peak, self.start_capital)
        print(f"  Max drawdown: ₹{self.max_drawdown:,.0f}  ({self.max_drawdown/peak*100:.1f}% of peak)")
        print(f"  Capital     : ₹{self.start_capital:,.0f} → ₹{self.final_capital:,.0f}  "
              f"({(self.final_capital-self.start_capital)/self.start_capital*100:+.1f}%)")
        print(sep)
        if self.trades:
            print(f"\n  {'#':<4} {'Date':<12} {'Opt':<4} {'Entry':>7} {'Exit':>7} "
                  f"{'P&L pts':>8} {'P&L ₹':>9} {'Reason':<18}")
            print(f"  {'─'*4} {'─'*12} {'─'*4} {'─'*7} {'─'*7} {'─'*8} {'─'*9} {'─'*18}")
            for i, t in enumerate(self.trades, 1):
                sign = "+" if t.pnl_pts >= 0 else ""
                print(f"  {i:<4} {t.date_str:<12} {t.option:<4} "
                      f"{t.opt_entry:>7.1f} {t.opt_exit:>7.1f} "
                      f"{sign}{t.pnl_pts:>7.1f} {sign}₹{t.pnl_rs:>7,.0f} "
                      f"{t.exit_reason:<18}")


# ── SMA helpers ───────────────────────────────────────────────────────────────

def _sma(candles: list[Candle], period: int) -> float | None:
    closes = [c[3] for c in candles]
    if len(closes) < period:
        return None
    return sum(closes[-period:]) / period


def _atr(candles: list[Candle], period: int = 14) -> float:
    if len(candles) < 2:
        return 30.0
    trs = []
    for i in range(1, len(candles)):
        h, l, pc = candles[i][1], candles[i][2], candles[i-1][3]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    use = trs[-period:] if len(trs) >= period else trs
    return round(sum(use) / len(use), 2) if use else 30.0


# ── Capital sizing (mirrors capital_service logic) ────────────────────────────

def _size_lots(capital: float, entry: float, sl: float, lot_size: int = 65) -> int:
    risk_per_lot = (entry - sl) * lot_size
    if risk_per_lot <= 0:
        return 0
    affordable = max(1, int(capital / (entry * lot_size)))
    if capital < 25_000:
        return min(1, affordable)
    elif capital < 1_00_000:
        max_risk = capital * 0.05
        return min(affordable, max(1, int(max_risk / risk_per_lot)))
    else:
        max_risk = capital * 0.02
        return min(affordable, max(1, int(max_risk / risk_per_lot)), 10)


# ── Main engine ───────────────────────────────────────────────────────────────

class BacktestEngine:
    """
    Replay historical Nifty 5-min candles and simulate trades.

    Signal: SMA score only (20 and 50 period).
      +2 = both above → CE signal
      -2 = both below → PE signal
      Other = no trade

    Entry: on signal bar close (simplified — no retest wait).
    Option price: approximated as spot × ATM_PCT.
    SL: ATR × atr_mult below/above entry.
    Target: SL distance × rr_ratio.
    Trail: milestone system (50% → 75% → 100% of target).
    """

    def __init__(
        self,
        capital:          float = 30_000,
        atr_mult:         float = 1.5,
        rr_ratio:         float = 2.0,
        min_score:        int   = 2,
        lot_size:         int   = 65,
        max_trades_day:   int   = 3,
        daily_loss_pct:   float = 2.0,
        cooldown_bars:    int   = 6,   # 30-min gap between entries (6 × 5-min bars)
        skip_open_bars:   int   = 6,   # skip first 30 min of session (false breakouts)
        consec_sl_limit:  int   = 3,   # pause rest of day after N consecutive SL hits
        max_lots:         int   = 10,  # hard cap on position size regardless of capital
    ):
        self.capital          = capital
        self.atr_mult         = atr_mult
        self.rr_ratio         = rr_ratio
        self.min_score        = min_score
        self.lot_size         = lot_size
        self.max_trades_day   = max_trades_day
        self.daily_loss_pct   = daily_loss_pct
        self.cooldown_bars    = cooldown_bars
        self.skip_open_bars   = skip_open_bars
        self.consec_sl_limit  = consec_sl_limit
        self.max_lots         = max_lots

    def run(self, candles: list[Candle], dates: list[str] | None = None) -> BacktestResult:
        """
        Run backtest over `candles` (5-min bars, oldest first).
        `dates` optional parallel list of date strings for display.
        """
        result        = BacktestResult(start_capital=self.capital, final_capital=self.capital)
        capital       = self.capital
        lot_size      = self.lot_size
        warmup        = 200   # need 200 bars for SMA200
        in_trade      = False
        day_trades    = 0
        day_bar_count = 0
        day_loss      = 0.0
        consec_sl     = 0
        prev_date     = ""
        last_exit_bar = -999   # cooldown tracker

        # Trade state
        opt_entry = sl = target = 0.0
        direction: str = ""
        peak_opt  = 0.0
        lots      = 0
        trade_idx = 0
        sl_floor  = 0.0
        trail_pct = 0.15
        phase     = 0

        for i in range(warmup, len(candles)):
            c     = candles[i]
            close = c[3]
            date_str = dates[i] if dates else str(i)

            # Reset daily counters on new day
            if date_str != prev_date:
                day_trades    = 0
                day_bar_count = 0
                day_loss      = 0.0
                consec_sl     = 0
                prev_date     = date_str
            day_bar_count += 1

            # ── Compute SMA score ──────────────────────────────────────────
            window = candles[max(0, i-199):i+1]
            s20  = _sma(window, 20)
            s50  = _sma(window, 50)
            sc20 = (1 if close > s20 else -1) if s20 else 0
            sc50 = (1 if close > s50 else -1) if s50 else 0
            score = sc20 + sc50   # -2, -1, 0, +1, +2

            option = "CE" if score >= self.min_score else "PE" if score <= -self.min_score else None

            atr = _atr(candles[max(0, i-14):i+1])

            # ── Manage open trade ──────────────────────────────────────────
            if in_trade:
                opt_now  = max(0.1, opt_entry + (close - candles[trade_idx][3]) * ATM_DELTA
                               * (1 if direction == "CE" else -1))
                peak_opt = max(peak_opt, opt_now)

                # ── Milestone trail SL ─────────────────────────────────────
                progress = (opt_now - opt_entry) / (target - opt_entry) if (target - opt_entry) > 0 else 0
                if phase < 1 and progress >= 0.50:
                    phase = 1; trail_pct = 0.15; sl_floor = opt_entry
                if phase < 2 and progress >= 0.75:
                    phase = 2; trail_pct = 0.12; sl_floor = opt_entry + (target - opt_entry) * 0.50
                if phase < 3 and progress >= 1.0:
                    phase = 3; trail_pct = 0.10; sl_floor = opt_entry + (target - opt_entry) * 0.75
                trail_sl = max(peak_opt * (1 - trail_pct), sl_floor)
                effective_sl = max(sl, trail_sl) if phase > 0 else sl

                # ── Check exit conditions ──────────────────────────────────
                exit_reason: str | None = None
                opt_exit = opt_now

                if opt_now <= effective_sl:
                    exit_reason = "sl_hit" if phase == 0 else "trail_sl"
                    opt_exit    = effective_sl
                elif opt_now >= target:
                    exit_reason = "target_hit"
                    opt_exit    = target
                elif option and option != direction:
                    exit_reason = "signal_flip"

                if exit_reason:
                    pnl_pts = opt_exit - opt_entry
                    pnl_rs  = pnl_pts * lots * lot_size
                    capital += pnl_rs
                    day_loss += min(0, pnl_rs)
                    consec_sl = consec_sl + 1 if pnl_pts < 0 else 0

                    result.trades.append(BtTrade(
                        idx=i, date_str=date_str, option=direction,
                        spot_entry=candles[trade_idx][3], opt_entry=opt_entry,
                        opt_sl=sl, opt_target=target,
                        spot_exit=close, opt_exit=opt_exit,
                        exit_reason=exit_reason,
                        pnl_pts=round(pnl_pts, 2),
                        pnl_rs=round(pnl_rs, 2),
                        lots=lots,
                        peak_pts=round(peak_opt - opt_entry, 2),
                    ))
                    in_trade      = False
                    last_exit_bar = i
                    result.final_capital = capital
                continue   # don't enter a new trade on same bar as exit

            # ── Check entry conditions ─────────────────────────────────────
            if not option:
                continue
            if day_trades >= self.max_trades_day:
                continue
            if day_loss <= -(capital * self.daily_loss_pct / 100):
                continue
            if i - last_exit_bar < self.cooldown_bars:
                continue
            if day_bar_count <= self.skip_open_bars:
                continue
            if consec_sl >= self.consec_sl_limit:
                continue

            # Estimate option entry price
            opt_entry  = round(close * ATM_PCT, 1)
            if opt_entry < 5:
                opt_entry = 5.0

            nifty_sl_pts = atr * self.atr_mult
            sl_opt       = round(opt_entry - ATM_DELTA * nifty_sl_pts, 1)
            sl_opt       = max(sl_opt, opt_entry * 0.3)   # floor at 30% of premium
            target_opt   = round(opt_entry + (opt_entry - sl_opt) * self.rr_ratio, 1)

            lots = min(_size_lots(capital, opt_entry, sl_opt, lot_size), self.max_lots)
            if lots == 0:
                continue

            # Enter trade
            in_trade  = True
            direction = option
            sl        = sl_opt
            target    = target_opt
            sl_floor  = sl_opt
            trail_pct = 0.15
            phase     = 0
            peak_opt  = opt_entry
            trade_idx = i
            day_trades += 1

        result.final_capital = capital
        return result
