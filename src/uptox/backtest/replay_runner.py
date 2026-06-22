"""
Replay backtest — uses real recorded market data from logs/market_data/*.jsonl
instead of approximated option prices.

Usage:
    uv run python -m uptox.backtest.replay_runner
    uv run python -m uptox.backtest.replay_runner --days 30 --capital 30000 --csv out.csv

Each JSONL row was recorded live at 5-min bar close and contains actual
CE/PE LTP, OI, PCR, cumulative delta, SMA scores — allowing full-fidelity
replay of what the system actually saw.

Signal logic mirrors dashboard:
  sma_score  : from stored sma_score field
  flow_score : sign of cum_delta (+1 / -1 / 0)
  pcr_filter : blocks CE if PCR < pcr_bull_block, blocks PE if PCR > pcr_bear_block
  total_score: sma_score + flow_score; entry if |score| >= min_score
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from datetime import date, timedelta

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger(__name__)

_DATA_DIR    = Path("logs/market_data")
ATM_DELTA    = 0.45
NIFTY_LOT    = 65


# ── Bar dataclass ─────────────────────────────────────────────────────────────

@dataclass
class Bar:
    ts:        str
    date_str:  str
    nifty:     float
    atm:       int
    ce_ltp:    float
    pe_ltp:    float
    ce_oi:     int
    pe_oi:     int
    pcr:       float
    cum_delta: float
    sma_score: int

    @classmethod
    def from_dict(cls, d: dict) -> "Bar":
        ts = d["ts"]
        return cls(
            ts        = ts,
            date_str  = ts[:10],
            nifty     = float(d.get("nifty", 0)),
            atm       = int(d.get("atm", 0)),
            ce_ltp    = float(d.get("ce_ltp", 0)),
            pe_ltp    = float(d.get("pe_ltp", 0)),
            ce_oi     = int(d.get("ce_oi", 0)),
            pe_oi     = int(d.get("pe_oi", 0)),
            pcr       = float(d.get("pcr", 0)),
            cum_delta = float(d.get("cum_delta", 0)),
            sma_score = int(d.get("sma_score", 0)),
        )


# ── Trade dataclass ───────────────────────────────────────────────────────────

@dataclass
class RpTrade:
    ts_entry:    str
    date_str:    str
    option:      str
    opt_entry:   float
    opt_sl:      float
    opt_target:  float
    opt_exit:    float
    exit_reason: str
    pnl_pts:     float
    pnl_rs:      float
    lots:        int
    peak_pts:    float


@dataclass
class ReplayResult:
    trades:        list[RpTrade] = field(default_factory=list)
    start_capital: float = 0.0
    final_capital: float = 0.0

    @property
    def wins(self):    return [t for t in self.trades if t.pnl_pts > 0]
    @property
    def losses(self):  return [t for t in self.trades if t.pnl_pts <= 0]
    @property
    def win_rate(self): return len(self.wins) / len(self.trades) * 100 if self.trades else 0
    @property
    def total_pnl_rs(self): return sum(t.pnl_rs for t in self.trades)
    @property
    def max_drawdown(self):
        peak = cap = self.start_capital
        dd = 0.0
        for t in self.trades:
            cap += t.pnl_rs
            peak = max(peak, cap)
            dd   = max(dd, peak - cap)
        return dd

    def print_summary(self) -> None:
        sep = "─" * 56
        print(sep)
        print("  REPLAY BACKTEST (real recorded data)")
        print(sep)
        print(f"  Trades      : {len(self.trades)}  ({len(self.wins)}W / {len(self.losses)}L)")
        print(f"  Win rate    : {self.win_rate:.1f}%")
        if self.trades:
            avg_w = sum(t.pnl_rs for t in self.wins)   / max(len(self.wins),   1)
            avg_l = sum(t.pnl_rs for t in self.losses) / max(len(self.losses), 1)
            rr    = abs(avg_w / avg_l) if avg_l else 0
            print(f"  Avg win     : ₹{avg_w:,.0f}")
            print(f"  Avg loss    : ₹{avg_l:,.0f}")
            print(f"  R:R ratio   : {rr:.2f}")
        print(f"  Total P&L   : ₹{self.total_pnl_rs:+,.0f}")
        peak = max(
            (self.start_capital + sum(t.pnl_rs for t in self.trades[:i+1]))
            for i in range(len(self.trades))
        ) if self.trades else self.start_capital
        peak = max(peak, self.start_capital)
        print(f"  Max drawdown: ₹{self.max_drawdown:,.0f}  ({self.max_drawdown/peak*100:.1f}% of peak)")
        print(f"  Capital     : ₹{self.start_capital:,.0f} → ₹{self.final_capital:,.0f}"
              f"  ({(self.final_capital-self.start_capital)/self.start_capital*100:+.1f}%)")
        print(sep)
        if self.trades:
            print(f"\n  {'#':<4} {'Date':<12} {'Time':<6} {'Opt':<4} {'Entry':>7} {'Exit':>7}"
                  f" {'P&L pts':>8} {'P&L ₹':>9} {'Reason'}")
            print(f"  {'─'*4} {'─'*12} {'─'*6} {'─'*4} {'─'*7} {'─'*7} {'─'*8} {'─'*9} {'─'*18}")
            for i, t in enumerate(self.trades, 1):
                sign = "+" if t.pnl_pts >= 0 else ""
                print(f"  {i:<4} {t.date_str:<12} {t.ts_entry[11:16]:<6} {t.option:<4}"
                      f" {t.opt_entry:>7.1f} {t.opt_exit:>7.1f}"
                      f" {sign}{t.pnl_pts:>7.1f} {sign}₹{t.pnl_rs:>7,.0f}"
                      f" {t.exit_reason}")


# ── Sizing (same tiers as capital_service) ────────────────────────────────────

def _size_lots(capital: float, entry: float, sl: float,
               lot_size: int = NIFTY_LOT, max_lots: int = 10) -> int:
    if entry <= 0 or sl >= entry:
        return 0
    lot_cost       = entry * lot_size
    affordable     = max(1, int(capital / lot_cost))
    sl_risk_per_lot = (entry - sl) * lot_size
    if sl_risk_per_lot <= 0:
        return 0
    if capital < 25_000:
        lots = 1
    elif capital < 1_00_000:
        lots = min(affordable, max(1, int(capital * 0.05 / sl_risk_per_lot)))
    else:
        lots = min(affordable, max(1, int(capital * 0.02 / sl_risk_per_lot)), max_lots)
    return lots


# ── Replay engine ─────────────────────────────────────────────────────────────

def run_replay(
    bars:            list[Bar],
    capital:         float = 30_000,
    atr_mult:        float = 1.5,
    rr_ratio:        float = 2.0,
    min_score:       int   = 2,
    max_trades_day:  int   = 3,
    daily_loss_pct:  float = 2.0,
    cooldown_bars:   int   = 6,
    skip_open_bars:  int   = 6,
    consec_loss_limit: int = 3,
    max_lots:        int   = 10,
    pcr_bull_block:  float = 0.5,   # block CE if PCR < this
    pcr_bear_block:  float = 2.0,   # block PE if PCR > this
) -> ReplayResult:
    result    = ReplayResult(start_capital=capital, final_capital=capital)
    cap       = capital
    in_trade  = False
    prev_date = ""
    day_trades    = 0
    day_bar_count = 0
    day_loss      = 0.0
    consec_loss   = 0
    last_exit_bar = -999

    opt_entry = sl = target = 0.0
    direction = ""
    peak_opt  = 0.0
    lots      = 0
    entry_ts  = ""
    phase     = 0
    sl_floor  = 0.0
    trail_pct = 0.15

    for i, bar in enumerate(bars):
        if not bar.nifty:
            continue

        if bar.date_str != prev_date:
            day_trades    = 0
            day_bar_count = 0
            day_loss      = 0.0
            consec_loss   = 0
            prev_date     = bar.date_str
        day_bar_count += 1

        # ── Manage open trade ──────────────────────────────────────────────────
        if in_trade:
            opt_price = bar.ce_ltp if direction == "CE" else bar.pe_ltp
            opt_now   = opt_price if opt_price else max(
                0.1, opt_entry + (bar.nifty - bars[i-1].nifty if i > 0 else 0) * ATM_DELTA
                     * (1 if direction == "CE" else -1)
            )
            peak_opt = max(peak_opt, opt_now)

            progress = (opt_now - opt_entry) / (target - opt_entry) if (target - opt_entry) > 0 else 0
            if phase < 1 and progress >= 0.50:
                phase = 1; trail_pct = 0.15; sl_floor = opt_entry
            if phase < 2 and progress >= 0.75:
                phase = 2; trail_pct = 0.12; sl_floor = opt_entry + (target - opt_entry) * 0.50
            if phase < 3 and progress >= 1.0:
                phase = 3; trail_pct = 0.10; sl_floor = opt_entry + (target - opt_entry) * 0.75
            trail_sl     = max(peak_opt * (1 - trail_pct), sl_floor)
            effective_sl = max(sl, trail_sl) if phase > 0 else sl

            exit_reason: str | None = None
            opt_exit = opt_now
            if opt_now <= effective_sl:
                exit_reason = "sl_hit" if phase == 0 else "trail_sl"
                opt_exit    = effective_sl
            elif opt_now >= target:
                exit_reason = "target_hit"
                opt_exit    = target
            else:
                # Signal flip check
                flow  = (1 if bar.cum_delta > 0 else -1) if bar.cum_delta else 0
                score = bar.sma_score + flow
                new_dir = "CE" if score >= min_score else "PE" if score <= -min_score else None
                if new_dir and new_dir != direction:
                    exit_reason = "signal_flip"

            if exit_reason:
                pnl_pts = round(opt_exit - opt_entry, 2)
                pnl_rs  = round(pnl_pts * lots * NIFTY_LOT, 0)
                cap    += pnl_rs
                day_loss += min(0, pnl_rs)
                consec_loss = consec_loss + 1 if pnl_pts < 0 else 0
                result.trades.append(RpTrade(
                    ts_entry=entry_ts, date_str=bar.date_str, option=direction,
                    opt_entry=opt_entry, opt_sl=sl, opt_target=target,
                    opt_exit=round(opt_exit, 2), exit_reason=exit_reason,
                    pnl_pts=pnl_pts, pnl_rs=pnl_rs, lots=lots,
                    peak_pts=round(peak_opt - opt_entry, 2),
                ))
                in_trade      = False
                last_exit_bar = i
                result.final_capital = cap
            continue

        # ── Entry conditions ───────────────────────────────────────────────────
        if day_trades >= max_trades_day:
            continue
        if day_bar_count <= skip_open_bars:
            continue
        if i - last_exit_bar < cooldown_bars:
            continue
        if day_loss <= -(cap * daily_loss_pct / 100):
            continue
        if consec_loss >= consec_loss_limit:
            continue

        flow  = (1 if bar.cum_delta > 0 else -1) if bar.cum_delta else 0
        score = bar.sma_score + flow
        if abs(score) < min_score:
            continue

        option = "CE" if score >= min_score else "PE"

        # PCR filter — use real recorded PCR
        if bar.pcr > 0:
            if option == "CE" and bar.pcr < pcr_bull_block:
                continue
            if option == "PE" and bar.pcr > pcr_bear_block:
                continue

        # Use real option LTP as entry price
        opt_ltp = bar.ce_ltp if option == "CE" else bar.pe_ltp
        if not opt_ltp or opt_ltp < 2:
            continue

        # ATR approximation from recent nifty range (no tick data stored)
        recent  = bars[max(0, i-14):i+1]
        nifty_range = max((b.nifty for b in recent), default=bar.nifty) - \
                      min((b.nifty for b in recent), default=bar.nifty)
        atr_est = max(nifty_range / 3, 20.0)

        sl_opt     = round(opt_ltp - ATM_DELTA * atr_est * atr_mult, 1)
        sl_opt     = max(sl_opt, opt_ltp * 0.30)
        target_opt = round(opt_ltp + (opt_ltp - sl_opt) * rr_ratio, 1)

        n = _size_lots(cap, opt_ltp, sl_opt, NIFTY_LOT, max_lots)
        if n == 0:
            continue

        in_trade  = True
        direction = option
        opt_entry = opt_ltp
        sl        = sl_opt
        target    = target_opt
        sl_floor  = sl_opt
        phase     = 0
        trail_pct = 0.15
        peak_opt  = opt_ltp
        lots      = n
        entry_ts  = bar.ts
        day_trades += 1

    result.final_capital = cap
    return result


# ── Data loader ───────────────────────────────────────────────────────────────

def load_bars(days: int) -> list[Bar]:
    today = date.today()
    bars: list[Bar] = []
    for d in range(days, -1, -1):
        day  = today - timedelta(days=d)
        path = _DATA_DIR / f"{day.isoformat()}.jsonl"
        if not path.exists():
            continue
        with path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    bars.append(Bar.from_dict(json.loads(line)))
                except Exception as e:
                    log.warning("bad row in %s: %s", path.name, e)
    log.info("Loaded %d bars from %d days of recorded data", len(bars), days)
    return bars


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser(description="Replay backtest on real recorded market data")
    p.add_argument("--days",              type=int,   default=30,    help="Calendar days to replay (default 30)")
    p.add_argument("--capital",           type=float, default=30000, help="Starting capital (default 30000)")
    p.add_argument("--atr-mult",          type=float, default=1.5)
    p.add_argument("--rr",                type=float, default=2.0)
    p.add_argument("--min-score",         type=int,   default=2)
    p.add_argument("--max-trades",        type=int,   default=3)
    p.add_argument("--daily-loss-pct",    type=float, default=2.0)
    p.add_argument("--cooldown-bars",     type=int,   default=6)
    p.add_argument("--skip-open-bars",    type=int,   default=6)
    p.add_argument("--consec-loss-limit", type=int,   default=3)
    p.add_argument("--max-lots",          type=int,   default=10)
    p.add_argument("--pcr-bull-block",    type=float, default=0.5)
    p.add_argument("--pcr-bear-block",    type=float, default=2.0)
    p.add_argument("--csv",               type=str,   default=None)
    args = p.parse_args()

    if not _DATA_DIR.exists():
        log.error("No recorded data found at %s — run live first to collect data", _DATA_DIR)
        sys.exit(1)

    bars = load_bars(args.days)
    if not bars:
        log.error("No bars loaded — check %s for YYYY-MM-DD.jsonl files", _DATA_DIR)
        sys.exit(1)

    result = run_replay(
        bars,
        capital=args.capital,
        atr_mult=args.atr_mult,
        rr_ratio=args.rr,
        min_score=args.min_score,
        max_trades_day=args.max_trades,
        daily_loss_pct=args.daily_loss_pct,
        cooldown_bars=args.cooldown_bars,
        skip_open_bars=args.skip_open_bars,
        consec_loss_limit=args.consec_loss_limit,
        max_lots=args.max_lots,
        pcr_bull_block=args.pcr_bull_block,
        pcr_bear_block=args.pcr_bear_block,
    )
    result.print_summary()

    # Monthly breakdown
    if result.trades:
        from collections import defaultdict
        monthly: dict[str, float] = defaultdict(float)
        for t in result.trades:
            monthly[t.date_str[:7]] += t.pnl_rs
        print("\n  Monthly P&L:")
        for month, pnl in sorted(monthly.items()):
            sign = "+" if pnl >= 0 else ""
            bar  = "█" * int(abs(pnl) / 500) if abs(pnl) >= 500 else "▌"
            print(f"    {month}  {sign}₹{pnl:>8,.0f}  {bar}")

    if args.csv and result.trades:
        with open(args.csv, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=[
                "ts_entry", "date", "option", "opt_entry", "opt_sl", "opt_target",
                "opt_exit", "exit_reason", "pnl_pts", "pnl_rs", "lots", "peak_pts",
            ])
            w.writeheader()
            for t in result.trades:
                w.writerow({
                    "ts_entry": t.ts_entry, "date": t.date_str, "option": t.option,
                    "opt_entry": t.opt_entry, "opt_sl": t.opt_sl, "opt_target": t.opt_target,
                    "opt_exit": t.opt_exit, "exit_reason": t.exit_reason,
                    "pnl_pts": t.pnl_pts, "pnl_rs": t.pnl_rs, "lots": t.lots,
                    "peak_pts": t.peak_pts,
                })
        log.info("CSV saved → %s", args.csv)


if __name__ == "__main__":
    main()
