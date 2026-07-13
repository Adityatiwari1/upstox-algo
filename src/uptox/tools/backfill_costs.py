"""
One-time (repeatable) backfill: fetch real broker charges for every closed
trade in logs/trades.json that's missing cost_rs, using Upstox's own
/v2/charges/brokerage endpoint — same source trade_book.py uses live going
forward. Safe to re-run; only touches trades where cost_rs is still null.

Usage:
    uv run python -m uptox.tools.backfill_costs            # do it
    uv run python -m uptox.tools.backfill_costs --dry-run   # preview only

Old trades (pre-qty-field, ~2026-06-18/25) had no `qty` recorded — qty is
derived from lots × 65 (Nifty lot size was constant then). A trade missing
option_key entirely (no instrument to price) is skipped and logged, not
guessed at.

Rate limits (Upstox, standard-API class): 50 req/s, 500/min — this script
does 2 requests/trade with a small delay between trades, nowhere near the
ceiling even for the full history.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s: %(message)s")
log = logging.getLogger("backfill_costs")

_TRADES_FILE = Path("logs/trades.json")
_NIFTY_LOT   = 65
_DELAY_S     = 0.15   # gentle pacing between trades, well under rate limits


async def _charges_for(order_service, option_key: str, qty: int,
                       entry_price: float, exit_price: float) -> float | None:
    buy  = await order_service.get_charges(option_key, qty, entry_price, "BUY")
    sell = await order_service.get_charges(option_key, qty, exit_price, "SELL")
    if buy is None and sell is None:
        return None
    return round((buy or 0.0) + (sell or 0.0), 2)


async def amain(dry_run: bool) -> None:
    from uptox.services import order_service

    if not _TRADES_FILE.exists():
        log.error("no %s found", _TRADES_FILE)
        return

    trades = json.loads(_TRADES_FILE.read_text(encoding="utf-8"))
    todo = [t for t in trades if t.get("outcome") != "OPEN" and t.get("cost_rs") is None]
    log.info("%d closed trades total, %d missing cost_rs", len(trades), len(todo))

    priced = skipped = failed = 0
    for t in todo:
        key = t.get("option_key", "")
        qty = t.get("qty") or (t.get("lots", 1) * _NIFTY_LOT)
        entry, exit_ = t.get("entry_price"), t.get("exit_price")

        if not key or not qty or not entry or not exit_:
            log.warning("SKIP %s (%s) — missing option_key/qty/prices, can't price",
                       t.get("id"), t.get("date"))
            skipped += 1
            continue

        if dry_run:
            log.info("[dry] would price %s  %s qty=%d  entry=%.1f exit=%.1f",
                     t["id"], key, qty, entry, exit_)
            continue

        cost = await _charges_for(order_service, key, qty, entry, exit_)
        if cost is None:
            log.warning("FAILED %s (%s %s) — charges API returned nothing "
                       "(expired contract likely no longer priceable)",
                       t["id"], t["date"], key)
            failed += 1
        else:
            t["cost_rs"]    = cost
            t["pnl_rs_net"] = round(t.get("pnl_rs", 0.0) - cost, 2)
            log.info("PRICED %s  cost=₹%.2f  net=₹%.2f", t["id"], cost, t["pnl_rs_net"])
            priced += 1

        await asyncio.sleep(_DELAY_S)

    if dry_run:
        log.info("DRY RUN — nothing written. %d would be priced, %d would be skipped.",
                 len(todo) - skipped, skipped)
        return

    _TRADES_FILE.write_text(json.dumps(trades, indent=2, ensure_ascii=False), encoding="utf-8")
    log.info("DONE — priced=%d  failed=%d  skipped=%d  (trades.json saved)",
             priced, failed, skipped)


def main() -> None:
    dry_run = "--dry-run" in sys.argv
    asyncio.run(amain(dry_run))


if __name__ == "__main__":
    main()
