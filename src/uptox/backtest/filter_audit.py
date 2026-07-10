"""
Filter false-negative audit — joins logs/blocked_entries.jsonl with
logs/market_data/*.jsonl and measures the forward Nifty move (in the blocked
trade's direction) 15/30 min after each block, grouped per filter.

  positive avg → the filter blocked moves that went the trade's way (costs money)
  negative avg → the blocked trades would have lost (filter earns its keep)

Usage:
    uv run python -m uptox.backtest.filter_audit
    uv run python -m uptox.backtest.filter_audit --since 2026-07-06 --min-n 3

Caveats: forward spot move is a proxy, not trade P&L (no premium, theta, or
trail modelling); market data is 5-min sampled; small-n buckets are noise —
rank filters by it, don't fine-tune thresholds off one bucket.
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path

_DATA_DIR    = Path("logs/market_data")
_BLOCKED     = Path("logs/blocked_entries.jsonl")


def _load_market_days() -> dict[str, list[tuple[datetime, float]]]:
    days: dict[str, list[tuple[datetime, float]]] = {}
    for f in sorted(_DATA_DIR.glob("*.jsonl")):
        rows = []
        for line in f.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            r = json.loads(line)
            if r.get("nifty"):
                rows.append((datetime.fromisoformat(r["ts"]), r["nifty"]))
        if rows:
            days[f.stem] = rows
    return days


def _fwd_move(rows: list[tuple[datetime, float]], ts: datetime, minutes: int) -> float | None:
    base = None
    target = None
    for t, px in rows:
        if base is None and t >= ts:
            base = px
            target = ts.timestamp() + minutes * 60
        if base is not None and t.timestamp() >= target:
            return px - base
    return None


def main() -> None:
    p = argparse.ArgumentParser(description="Blocked-entry filter audit")
    p.add_argument("--since",  type=str, default="2026-07-06",
                   help="Earliest date (YYYY-MM-DD) to include")
    p.add_argument("--min-n",  type=int, default=1,
                   help="Hide filter buckets with fewer samples than this")
    args = p.parse_args()

    md = _load_market_days()
    groups: dict[str, list[tuple[float, float | None]]] = defaultdict(list)

    for line in _BLOCKED.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        day = r["ts"][:10]
        if day < args.since or day not in md:
            continue
        ts   = datetime.fromisoformat(r["ts"])
        # Strip live numbers so "Retreat active -34pts" buckets with -61pts etc.
        key  = "".join(c for c in r["reason"].split("—")[0].split("(")[0] if not c.isdigit())
        key  = " ".join(key.split())[:36].strip()
        sign = 1 if r["option"] == "CE" else -1
        m15  = _fwd_move(md[day], ts, 15)
        if m15 is None:
            continue
        m30 = _fwd_move(md[day], ts, 30)
        groups[key].append((sign * m15, (sign * m30) if m30 is not None else None))

    print(f"{'filter':<38} {'n':>4} {'avg15':>7} {'avg30':>7} {'fav15%':>7}")
    print("-" * 68)
    out = []
    for key, vals in groups.items():
        if len(vals) < args.min_n:
            continue
        m15s = [v[0] for v in vals]
        m30s = [v[1] for v in vals if v[1] is not None]
        avg15 = sum(m15s) / len(m15s)
        avg30 = sum(m30s) / len(m30s) if m30s else avg15
        fav   = sum(1 for v in m15s if v > 0) / len(m15s) * 100
        out.append((avg30, key, len(m15s), avg15, fav))
    for avg30, key, n, avg15, fav in sorted(out, reverse=True):
        print(f"{key:<38} {n:>4} {avg15:>+7.1f} {avg30:>+7.1f} {fav:>6.0f}%")
    print("\npositive avg = filter blocked winners (costs money)")
    print("negative avg = filter blocked losers (earns its keep)")


if __name__ == "__main__":
    main()
