"""
Look up real broker charges for one trade — via Upstox's own
/v2/charges/brokerage API (same source trade_book.py uses live).

Usage:
    # by trade id from logs/trades.json
    uv run python -m uptox.tools.get_brokerage --id 673b2e87

    # by hand — any instrument, qty, entry/exit price (doesn't need to exist
    # in trades.json; useful for checking a hypothetical trade before taking it)
    uv run python -m uptox.tools.get_brokerage --token NSE_FO|51379 --qty 65 --entry 112.5 --exit 114.8
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
from pathlib import Path

logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")

_TRADES_FILE = Path("logs/trades.json")
_NIFTY_LOT   = 65


def _find_trade(trade_id: str) -> dict | None:
    if not _TRADES_FILE.exists():
        return None
    trades = json.loads(_TRADES_FILE.read_text(encoding="utf-8"))
    for t in trades:
        if t.get("id") == trade_id:
            return t
    return None


async def amain(args) -> None:
    from uptox.services import order_service

    if args.id:
        t = _find_trade(args.id)
        if not t:
            print(f"no trade with id '{args.id}' in {_TRADES_FILE}")
            return
        token = t.get("option_key", "")
        qty   = t.get("qty") or (t.get("lots", 1) * _NIFTY_LOT)
        entry = t.get("entry_price")
        exitp = t.get("exit_price")
        if not exitp:
            print(f"trade {args.id} is still OPEN — no exit price yet, showing entry-side charges only")
        print(f"Trade {args.id}: {t.get('option')} {t.get('strike')}  "
              f"{token}  qty={qty}  entry={entry}  exit={exitp or '—'}")
    else:
        if not (args.token and args.qty and args.entry):
            print("need either --id, or --token/--qty/--entry (and optionally --exit)")
            return
        token, qty, entry, exitp = args.token, args.qty, args.entry, args.exit

    buy = await order_service.get_charges(token, qty, entry, "BUY", product=args.product)
    if buy is None:
        print(f"BUY charges: lookup failed (invalid/expired instrument key, or bad token)")
    else:
        print(f"BUY  charges: ₹{buy:.2f}")

    sell = None
    if exitp:
        sell = await order_service.get_charges(token, qty, exitp, "SELL", product=args.product)
        if sell is None:
            print(f"SELL charges: lookup failed")
        else:
            print(f"SELL charges: ₹{sell:.2f}")

    if buy is not None or sell is not None:
        total = round((buy or 0.0) + (sell or 0.0), 2)
        print(f"{'ROUND TRIP' if sell is not None else 'ENTRY ONLY'} total: ₹{total:.2f}")


def main() -> None:
    p = argparse.ArgumentParser(description="Look up real broker charges for a trade")
    p.add_argument("--id",      type=str, default="", help="trade id from logs/trades.json")
    p.add_argument("--token",   type=str, default="", help="instrument key, e.g. NSE_FO|51379")
    p.add_argument("--qty",     type=int, default=0)
    p.add_argument("--entry",   type=float, default=0.0)
    p.add_argument("--exit",    type=float, default=0.0, help="omit for entry-side charges only")
    p.add_argument("--product", type=str, default="I", help="I=intraday (default), D=delivery")
    args = p.parse_args()
    asyncio.run(amain(args))


if __name__ == "__main__":
    main()
