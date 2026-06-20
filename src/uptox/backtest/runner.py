"""
Backtest runner — fetches Nifty historical candles and runs BacktestEngine.

Usage:
    uv run python -m uptox.backtest.runner
    uv run python -m uptox.backtest.runner --days 60 --capital 30000
    uv run python -m uptox.backtest.runner --days 90 --capital 50000 --csv results.csv
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import logging
import sys
from datetime import date, timedelta

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger(__name__)

NIFTY = "NSE_INDEX|Nifty 50"


async def _fetch_candles(access_token: str, days: int) -> list[tuple]:
    """Fetch Nifty 5-min candles via sma_service machinery."""
    from uptox.services.sma_service import _fetch_raw, _aggregate, TimeFrame, _TF_MINUTES
    raw = await _fetch_raw(NIFTY, "1minute", days, access_token)
    if not raw:
        log.error("No candle data returned — check token / connectivity")
        sys.exit(1)
    oldest_first = list(reversed(raw))
    bars = _aggregate(oldest_first, _TF_MINUTES[TimeFrame.M5])
    log.info("Fetched %d  5-min bars (%d days)", len(bars), days)
    return bars


_CHUNK_DAYS = 30   # Upstox 1-min API max range per request


async def _fetch_with_dates(access_token: str, days: int):
    """Return (candles, dates) where dates is bar-aligned date strings.

    Fetches in 30-day chunks (Upstox 1-min API limit) and concatenates.
    """
    import httpx
    from urllib.parse import quote
    from datetime import date, timedelta

    headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}
    sym     = quote(NIFTY, safe="")
    today   = date.today()

    # Build non-overlapping date windows oldest→newest
    windows: list[tuple[date, date]] = []
    end = today
    remaining = days
    while remaining > 0:
        chunk = min(remaining, _CHUNK_DAYS)
        start = end - timedelta(days=chunk)
        windows.append((start, end))
        end = start - timedelta(days=1)
        remaining -= chunk
    windows.reverse()   # oldest first

    all_raw: list = []
    async with httpx.AsyncClient(timeout=30) as c:
        for frm, to in windows:
            url = (
                f"https://api.upstox.com/v2/historical-candle"
                f"/{sym}/1minute/{to.isoformat()}/{frm.isoformat()}"
            )
            r = await c.get(url, headers=headers)
            if r.status_code != 200:
                log.error("Candle fetch failed %s for %s→%s: %s",
                          r.status_code, frm, to, r.text[:200])
                sys.exit(1)
            chunk_raw = r.json().get("data", {}).get("candles", [])
            log.info("  chunk %s→%s: %d raw 1-min bars", frm, to, len(chunk_raw))
            # API returns newest-first; reverse to oldest-first before extend
            all_raw.extend(reversed(chunk_raw))

    if not all_raw:
        log.error("Empty candle response across all chunks")
        sys.exit(1)

    # Aggregate 5 × 1-min bars → 1 × 5-min bar
    # Each raw row: [timestamp, o, h, l, c, volume, oi]
    interval = 5
    dates:   list[str]           = []
    candles: list[tuple[float, float, float, float]] = []
    for i in range(0, len(all_raw) - interval + 1, interval):
        group = all_raw[i:i + interval]
        ts    = group[0][0][:10]   # "2026-06-10T09:15:00+05:30" → "2026-06-10"
        o     = float(group[0][1])
        h     = max(float(row[2]) for row in group)
        l     = min(float(row[3]) for row in group)
        c     = float(group[-1][4])
        dates.append(ts)
        candles.append((o, h, l, c))

    log.info("Aggregated to %d  5-min bars from %d total 1-min bars", len(candles), len(all_raw))
    return candles, dates


async def _main(args, token: str):
    # ── Fetch data ────────────────────────────────────────────────────────────
    log.info("Fetching %d days of Nifty 5-min data...", args.days)
    candles, dates = await _fetch_with_dates(token, args.days)

    if len(candles) < 210:
        log.error("Not enough candles (%d) — need ≥210 for SMA200 warmup", len(candles))
        sys.exit(1)

    # ── Run backtest ──────────────────────────────────────────────────────────
    from uptox.backtest.engine import BacktestEngine
    engine = BacktestEngine(
        capital=args.capital,
        atr_mult=args.atr_mult,
        rr_ratio=args.rr,
        min_score=args.min_score,
        daily_loss_pct=args.daily_loss_pct,
        max_trades_day=args.max_trades,
        cooldown_bars=args.cooldown_bars,
        skip_open_bars=args.skip_open_bars,
        consec_sl_limit=args.consec_sl_limit,
        max_lots=args.max_lots,
    )
    result = engine.run(candles, dates)
    result.print_summary()

    # ── Monthly breakdown ─────────────────────────────────────────────────────
    if result.trades:
        from collections import defaultdict
        monthly: dict[str, float] = defaultdict(float)
        for t in result.trades:
            month = t.date_str[:7]   # "2026-06"
            monthly[month] += t.pnl_rs
        print("\n  Monthly P&L:")
        for month, pnl in sorted(monthly.items()):
            sign = "+" if pnl >= 0 else ""
            bar  = "█" * int(abs(pnl) / 500) if abs(pnl) >= 500 else "▌"
            print(f"    {month}  {sign}₹{pnl:>8,.0f}  {bar}")

    # ── CSV export ────────────────────────────────────────────────────────────
    if args.csv and result.trades:
        with open(args.csv, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=[
                "idx", "date", "option", "spot_entry", "opt_entry",
                "opt_sl", "opt_target", "spot_exit", "opt_exit",
                "exit_reason", "pnl_pts", "pnl_rs", "lots", "peak_pts",
            ])
            w.writeheader()
            for t in result.trades:
                w.writerow({
                    "idx": t.idx, "date": t.date_str, "option": t.option,
                    "spot_entry": t.spot_entry, "opt_entry": t.opt_entry,
                    "opt_sl": t.opt_sl, "opt_target": t.opt_target,
                    "spot_exit": t.spot_exit, "opt_exit": t.opt_exit,
                    "exit_reason": t.exit_reason, "pnl_pts": t.pnl_pts,
                    "pnl_rs": t.pnl_rs, "lots": t.lots, "peak_pts": t.peak_pts,
                })
        log.info("CSV saved → %s", args.csv)


def _ensure_token() -> str:
    """Validate stored token or re-authenticate synchronously (safe outside asyncio)."""
    from uptox.config.logging_config import setup as _setup
    _setup()
    from uptox.config.settings import settings
    from uptox.api.auth import fetch_access_token
    from uptox.services.token_service import save_access_token
    import httpx

    def _valid(tok: str) -> bool:
        try:
            r = httpx.get(
                "https://api.upstox.com/v2/user/profile",
                headers={"Authorization": f"Bearer {tok}"},
                timeout=8,
            )
            return r.status_code == 200
        except Exception:
            return False

    if settings.access_token and _valid(settings.access_token):
        log.info("Existing token valid")
        return settings.access_token

    if settings.access_token:
        log.warning("Token expired — re-authenticating")
    else:
        log.info("No access token — logging in")

    token = fetch_access_token()
    save_access_token(token)
    settings.access_token = token
    log.info("Login successful")
    return token


def main():
    p = argparse.ArgumentParser(description="Uptox backtest runner")
    p.add_argument("--days",           type=int,   default=60,    help="Calendar days of history (default 60)")
    p.add_argument("--capital",        type=float, default=30000, help="Starting capital ₹ (default 30000)")
    p.add_argument("--atr-mult",       type=float, default=1.5,   help="ATR SL multiplier (default 1.5)")
    p.add_argument("--rr",             type=float, default=2.0,   help="Risk:reward ratio (default 2.0)")
    p.add_argument("--min-score",      type=int,   default=2,     help="Min SMA score to trade (default 2)")
    p.add_argument("--daily-loss-pct", type=float, default=3.0,   help="Daily loss limit %% (default 3.0)")
    p.add_argument("--max-trades",     type=int,   default=3,     help="Max trades per day (default 3)")
    p.add_argument("--cooldown-bars",   type=int,   default=6,     help="Min bars between entries, 1 bar=5min (default 6=30min)")
    p.add_argument("--skip-open-bars", type=int,   default=6,     help="Skip first N bars of session to avoid open volatility (default 6=30min)")
    p.add_argument("--consec-sl-limit",type=int,   default=3,     help="Pause rest of day after N consecutive SL hits (default 3)")
    p.add_argument("--max-lots",       type=int,   default=10,    help="Hard cap on lots per trade regardless of capital (default 10)")
    p.add_argument("--csv",            type=str,   default=None,  help="Export trades to CSV file")
    args = p.parse_args()
    token = _ensure_token()
    asyncio.run(_main(args, token))


if __name__ == "__main__":
    main()
