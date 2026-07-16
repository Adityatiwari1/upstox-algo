"""
Focus-mode shadow comparison — per day, what the OLD (pre-focus) system would
have traded vs what the current system actually traded.

Old mode = every entry type enabled. Focus mode logs each benched trigger as
"Focus mode — <type> disabled" in logs/blocked_entries.jsonl, so the old-mode
day is reconstructed as: the actual trades (their entry types were available
in old mode too) PLUS the focus-blocked entries taken sequentially whenever
the (single-position) system was flat, with the standard 5-min cooldown.

Shadow exits are simulated on the 5-min spot path from logs/market_data:
    SL     signed spot move ≤ -20pt from entry   (≈ the -10 option-pt MAE stop)
    trail  after peak ≥ +8pt, exit at 50% retrace of peak (≈ profit-lock keep)
    eod    force-close on the last bar
Option P&L is estimated at 0.5 delta on 1 lot (65) minus ₹69 avg fees.

Usage:
    uv run python -m uptox.backtest.focus_compare
    uv run python -m uptox.backtest.focus_compare --since 2026-07-16 --date 2026-07-16

Caveats: 5-min sampling, flat 0.5 delta (no gamma/IV/theta), entries assumed
filled at the logged block-time spot. Estimates rank days and entry types —
don't read single-trade rupee figures as fact. Trades the NEW system takes
that old mode would have blocked (e.g. IV rank ≥70 after the gate was
disabled 2026-07-16) are not netted out: IV rank at entry isn't logged.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

_DATA_DIR = Path("logs/market_data")
_BLOCKED  = Path("logs/blocked_entries.jsonl")
_TRADES   = Path("logs/trades.json")

_DELTA        = 0.5    # flat option-delta estimate for shadow P&L
_LOT          = 65
_FEES_RS      = 69.0   # observed avg round-trip cost
_SL_SPOT_PTS  = 20.0   # ≈ -10 option pts, mirrors the MAE stop
_TRAIL_ARM    = 8.0    # signed spot pts favorable before trail arms
_TRAIL_KEEP   = 0.5    # exit at 50% retrace of peak, mirrors profit lock
_COOLDOWN_S   = 300.0


def _load_day_path(day: str) -> list[tuple[float, float]]:
    f = _DATA_DIR / f"{day}.jsonl"
    if not f.exists():
        return []
    rows = []
    for line in f.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        if r.get("nifty"):
            rows.append((datetime.fromisoformat(r["ts"]).timestamp(), r["nifty"]))
    return rows


def _simulate(path: list[tuple[float, float]], entry_ts: float,
              entry_spot: float, sign: int) -> tuple[float, float, str] | None:
    """Walk the 5-min path from entry; return (exit_ts, signed_spot_pts, reason)."""
    peak = 0.0
    last = None
    for ts, px in path:
        if ts <= entry_ts:
            continue
        move = sign * (px - entry_spot)
        peak = max(peak, move)
        if move <= -_SL_SPOT_PTS:
            # stop fills near the trigger intrabar; the 5-min bar overshoots
            return ts, -_SL_SPOT_PTS, "sl"
        if peak >= _TRAIL_ARM and move <= peak * _TRAIL_KEEP:
            return ts, move, "trail"
        last = (ts, move)
    if last:
        return last[0], last[1], "eod"
    return None


def _rs(spot_pts: float) -> float:
    return spot_pts * _DELTA * _LOT - _FEES_RS


def _day_report(day: str, blocked: list[dict], trades: list[dict]) -> None:
    path = _load_day_path(day)
    actual = [t for t in trades if t.get("date") == day and t.get("exit_ts")]
    focus_rows = sorted(
        (r for r in blocked if r["ts"][:10] == day
         and r["reason"].startswith("Focus mode")),
        key=lambda r: r["ts"],
    )
    if not actual and not focus_rows:
        return

    # Old mode holds one position: busy during actual trades and shadow trades.
    # 120s pre-entry margin: a benched trigger seconds before an actual entry
    # is the same trade idea — old mode takes one of them, not both.
    busy: list[tuple[float, float]] = [
        (t["entry_ts"] - 120.0, t["exit_ts"] + _COOLDOWN_S) for t in actual
    ]
    shadows = []
    for r in focus_rows:
        ts = datetime.fromisoformat(r["ts"]).timestamp()
        if any(a <= ts <= b for a, b in busy):
            continue
        sign = 1 if r["option"] == "CE" else -1
        sim = _simulate(path, ts, r["ltp"], sign)
        if sim is None:
            continue
        exit_ts, pts, why = sim
        shadows.append((r, pts, why, exit_ts))
        busy.append((ts, exit_ts + _COOLDOWN_S))

    actual_net = sum(t.get("pnl_rs_net") or t.get("pnl_rs", 0) for t in actual)
    shadow_net = sum(_rs(pts) for _, pts, _, _ in shadows)

    print(f"\n=== {day} ===")
    print(f"ACTUAL (focus mode): {len(actual)} trade(s), net {actual_net:+,.0f}")
    for t in actual:
        print(f"  {t['time_entry'][:5]} {t['option']} {t.get('entry_reason','').split(' |')[0]:<14}"
              f" {t.get('pnl_pts', 0):+.1f}opt  Rs{t.get('pnl_rs_net') or t.get('pnl_rs', 0):+,.0f}")
    print(f"SHADOW (old mode would also have): {len(shadows)} trade(s), est {shadow_net:+,.0f}")
    for r, pts, why, exit_ts in shadows:
        et = datetime.fromtimestamp(exit_ts).strftime("%H:%M")
        print(f"  {r['ts'][11:16]} {r['option']} {r.get('entry_type') or '?':<14}"
              f" spot {r['ltp']:.1f} -> {why}@{et}  {pts:+.1f}sp"
              f" = {pts * _DELTA:+.1f}opt  Rs{_rs(pts):+,.0f}")
    print(f"OLD MODE est total: {actual_net + shadow_net:+,.0f}"
          f"   vs FOCUS actual: {actual_net:+,.0f}"
          f"   (focus {'kept' if shadow_net < 0 else 'gave up'} Rs{abs(shadow_net):,.0f})")


def main() -> None:
    p = argparse.ArgumentParser(description="Focus-mode vs old-mode shadow comparison")
    p.add_argument("--since", type=str, default="2026-07-16",
                   help="Earliest date (YYYY-MM-DD); focus mode went live 2026-07-16")
    p.add_argument("--date",  type=str, default=None,
                   help="Single date (YYYY-MM-DD) to report")
    args = p.parse_args()

    blocked = [json.loads(l) for l in _BLOCKED.read_text(encoding="utf-8").splitlines()
               if l.strip()]
    trades  = json.loads(_TRADES.read_text(encoding="utf-8"))

    days = sorted({r["ts"][:10] for r in blocked} | {t["date"] for t in trades})
    for day in days:
        if args.date and day != args.date:
            continue
        if day < args.since:
            continue
        _day_report(day, blocked, trades)
    print("\nShadow P&L = 5-min spot path x 0.5 delta - Rs69 fees; estimate, not fact.")


if __name__ == "__main__":
    main()
