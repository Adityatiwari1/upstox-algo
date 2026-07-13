"""
Attended live-API smoke test — daily 2026-07-13 → 07-17 until live trading is
trusted. Runs STANDALONE while the main app paper-trades in parallel; the two
share nothing but the access token.

Usage:
    uv run python -m uptox.tools.smoke_test --dry-run                      # rehearse, no HTTP
    uv run python -m uptox.tools.smoke_test --expiry 2026-07-14            # happy + negative paths
    uv run python -m uptox.tools.smoke_test --expiry 2026-07-14 --money-test

Safety rails:
  - deep-OTM weekly option, premium band ₹5-15 (abort above --max-prem 20)
  - exactly 1 lot, every order tagged "uptox-smoke"
  - Enter-confirm before every order; q aborts and FLATTENS whatever the
    script opened (cancel resting SL, market-out any position)
  - every request/response appended to logs/smoke_test_<date>.log
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from uptox.services import order_service as osvc

_IST      = ZoneInfo("Asia/Kolkata")
_CHAIN    = "https://api.upstox.com/v2/option/chain"
_CONTRACT = "https://api.upstox.com/v2/option/contract"
_NIFTY    = "NSE_INDEX|Nifty 50"
_TAG      = "uptox-smoke"

log = logging.getLogger("smoke_test")

# script-held state so an abort can always flatten
_state = {"pos_qty": 0, "pos_key": "", "sl_order_id": "", "last_ltp": 0.0,
          "entry_order_id": ""}


def _setup_logging() -> Path:
    p = Path(f"logs/smoke_test_{datetime.now(_IST):%Y%m%d_%H%M}.log")
    p.parent.mkdir(exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        handlers=[logging.FileHandler(p, encoding="utf-8"),
                  logging.StreamHandler(sys.stdout)],
    )
    return p


def _confirm(step: str) -> bool:
    """Enter = go, s = skip, q = abort-and-flatten."""
    ans = input(f"\n>>> {step}   [Enter=go / s=skip / q=abort] ").strip().lower()
    if ans == "q":
        raise KeyboardInterrupt
    return ans != "s"


async def _poll_terminal(order_id: str, want: tuple[str, ...],
                         timeout_s: float = 60.0) -> dict:
    """Poll order details every 2s until status is in `want` (or terminal-bad).
    Returns the final details dict."""
    end = asyncio.get_event_loop().time() + timeout_s
    while True:
        d = await osvc.get_order_details(order_id)
        st = (d.get("status") or "").lower()
        log.info("poll %s → status=%s filled=%s avg=%s trigger=%s",
                 order_id, st, d.get("filled_quantity"),
                 d.get("average_price"), d.get("trigger_price"))
        if st in want or st in ("rejected", "cancelled", "complete"):
            return d
        if asyncio.get_event_loop().time() > end:
            log.warning("poll timeout for %s (last status=%s)", order_id, st)
            return d
        await asyncio.sleep(2.0)


def _order_id(resp: dict) -> str:
    ids = (resp.get("data") or {}).get("order_ids", [])
    return ids[0] if ids else ""


def _auth_headers() -> dict:
    from uptox.config.settings import settings
    return {"Authorization": f"Bearer {settings.access_token}",
            "Accept": "application/json"}


async def _list_expiries() -> list[str]:
    """Valid Nifty option expiry dates from today onward, soonest first."""
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get(_CONTRACT, params={"instrument_key": _NIFTY},
                        headers=_auth_headers())
    rows = r.json().get("data") or []
    today = datetime.now(_IST).strftime("%Y-%m-%d")
    return sorted({row.get("expiry") for row in rows
                   if (row.get("expiry") or "") >= today})


async def _pick_strike(expiry: str, side: str, min_prem: float, max_prem: float) -> tuple[str, int, float]:
    """Deep-OTM strike in the premium band with the most OI. Returns
    (instrument_key, strike, ltp)."""
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get(_CHAIN, params={"instrument_key": _NIFTY, "expiry_date": expiry},
                        headers=_auth_headers())
    rows = r.json().get("data") or []
    if not rows:
        valid = await _list_expiries()
        raise RuntimeError(
            f"no option chain for expiry {expiry} — valid expiries: {valid[:6]}"
            f" (omit --expiry to auto-pick)"
        )
    cands = []
    for row in rows:
        leg = row.get("call_options" if side == "CE" else "put_options") or {}
        md  = leg.get("market_data") or {}
        ltp, oi = float(md.get("ltp") or 0), float(md.get("oi") or 0)
        key = leg.get("instrument_key")
        if key and min_prem <= ltp <= max_prem:
            cands.append((oi, key, int(row.get("strike_price", 0)), ltp))
    if not cands:
        raise RuntimeError(f"no {side} strike with premium ₹{min_prem}-{max_prem} — market too hot/cold, adjust band")
    oi, key, strike, ltp = max(cands)
    return key, strike, ltp


async def _flatten() -> None:
    """Abort path: cancel any still-resting ENTRY order, cancel resting SL,
    market-out any filled position. An unfilled entry (e.g. thin liquidity on
    a far-OTM strike, seen 2026-07-13: BUY timed out at 'open' with 0 filled)
    would otherwise sit live at the exchange with nothing here to cancel it."""
    if _state["entry_order_id"]:
        oid = _state["entry_order_id"]
        d = await osvc.get_order_details(oid)
        st = (d.get("status") or "").lower()
        if st not in ("complete", "cancelled", "rejected"):
            log.warning("FLATTEN: cancelling unfilled ENTRY order %s (status=%s)", oid, st)
            try:
                await osvc.cancel_order(oid)
            except Exception as e:
                log.error("FLATTEN: entry cancel failed (%s) — CHECK BROKER APP: order %s", e, oid)
        else:
            log.info("FLATTEN: entry order %s already %s, nothing to cancel", oid, st)
        _state["entry_order_id"] = ""
    if _state["sl_order_id"]:
        log.warning("FLATTEN: cancelling SL order %s", _state["sl_order_id"])
        try:
            await osvc.cancel_order(_state["sl_order_id"])
        except Exception as e:
            log.warning("FLATTEN: cancel failed (%s) — may already be filled", e)
        _state["sl_order_id"] = ""
    if _state["pos_qty"] > 0:
        px = round(max(_state["last_ltp"] * 0.90, 0.1), 1)
        log.warning("FLATTEN: selling %d of %s LIMIT %.1f (deep marketable)",
                    _state["pos_qty"], _state["pos_key"], px)
        try:
            resp = await osvc.place_order(_state["pos_key"], "SELL", _state["pos_qty"],
                                          price=px, tag=_TAG)
            await _poll_terminal(_order_id(resp), ("complete",))
            _state["pos_qty"] = 0
        except Exception as e:
            log.error("FLATTEN SELL FAILED: %s — CLOSE MANUALLY IN BROKER APP NOW", e)


async def run_happy_path(key: str, strike: int, ltp: float, qty: int) -> None:
    _state.update(pos_key=key, last_ltp=ltp)

    # 1. entry
    if _confirm(f"STEP 1  BUY {qty} × {strike} @ LIMIT {round(ltp*1.10,1)} (marketable)"):
        resp = await osvc.place_order(key, "BUY", qty, price=round(ltp * 1.10, 1), tag=_TAG)
        _state["entry_order_id"] = _order_id(resp)
        d = await _poll_terminal(_state["entry_order_id"], ("complete",), timeout_s=90.0)
        if (d.get("status") or "").lower() != "complete":
            raise RuntimeError(f"entry did not fill: {d}")
        _state["entry_order_id"] = ""   # filled — nothing left for flatten to cancel
        _state["pos_qty"] = qty
        fill = float(d.get("average_price") or ltp)
        log.info("ENTRY FILLED @ %.2f", fill)
    else:
        fill = ltp

    # 2. resting SL (stop-limit with OUR band — SL-M gets silently converted
    #    on NSE options, seen 2026-07-13)
    trig = round(fill * 0.70, 1)
    limit = osvc.sl_limit_price(trig)
    if _confirm(f"STEP 2  place SL SELL trigger {trig} / limit {limit} (30%% below fill)"):
        resp = await osvc.place_order(key, "SELL", qty, price=limit, order_type="SL",
                                      trigger_price=trig, tag=_TAG)
        _state["sl_order_id"] = _order_id(resp)
        d = await _poll_terminal(_state["sl_order_id"], ("trigger pending", "open"))
        log.info("SL RESTING: id=%s status=%s trigger=%s limit=%s",
                 _state["sl_order_id"], d.get("status"),
                 d.get("trigger_price"), d.get("price"))

    # 3+4. trail steps — modify trigger up twice, band moves with it
    for i, frac in enumerate((0.75, 0.80), start=3):
        new_trig  = round(fill * frac, 1)
        new_limit = osvc.sl_limit_price(new_trig)
        if _state["sl_order_id"] and _confirm(f"STEP {i}  MODIFY trigger → {new_trig} / limit {new_limit}"):
            await osvc.modify_order(_state["sl_order_id"], qty, new_trig)
            d = await osvc.get_order_details(_state["sl_order_id"])
            got_t = float(d.get("trigger_price") or 0)
            got_p = float(d.get("price") or 0)
            log.info("VERIFY trigger %.1f limit %.1f — %s",
                     got_t, got_p,
                     "PASS" if abs(got_t - new_trig) < 0.51 and abs(got_p - new_limit) < 0.51
                     else "FAIL")

    # 5. cancel SL
    if _state["sl_order_id"] and _confirm("STEP 5  CANCEL the SL"):
        await osvc.cancel_order(_state["sl_order_id"])
        d = await _poll_terminal(_state["sl_order_id"], ("cancelled",))
        log.info("CANCEL %s", "PASS" if (d.get("status") or "").lower() == "cancelled" else f"FAIL: {d}")
        _state["sl_order_id"] = ""

    # 6. exit
    if _state["pos_qty"] and _confirm(f"STEP 6  SELL {qty} @ LIMIT {round(fill*0.95,1)} (marketable)"):
        resp = await osvc.place_order(key, "SELL", qty, price=round(fill * 0.95, 1), tag=_TAG)
        d = await _poll_terminal(_order_id(resp), ("complete",))
        if (d.get("status") or "").lower() == "complete":
            _state["pos_qty"] = 0
            log.info("EXIT FILLED @ %s", d.get("average_price"))

    # 7. broker ground truth
    if _confirm("STEP 7  GET positions — verify flat"):
        pos = await osvc.get_positions()
        open_qty = sum(abs(int(p.get("quantity") or 0)) for p in pos
                       if p.get("instrument_token") == key)
        log.info("POSITIONS: %d rows, this contract qty=%d — %s",
                 len(pos), open_qty, "PASS (flat)" if open_qty == 0 else "FAIL — NOT FLAT")


async def run_negative_paths() -> None:
    """Error handling on safe, guaranteed-invalid inputs (nothing can execute)."""
    if _confirm("NEG 1  cancel bogus order id — expect clean API error"):
        try:
            await osvc.cancel_order("SMOKE-BOGUS-1")
            log.info("NEG 1 FAIL — bogus cancel did not raise")
        except Exception as e:
            log.info("NEG 1 PASS — raised as expected: %s", e)
    if _confirm("NEG 2  modify bogus order id — expect clean API error"):
        try:
            await osvc.modify_order("SMOKE-BOGUS-2", 65, 5.0)
            log.info("NEG 2 FAIL — bogus modify did not raise")
        except Exception as e:
            log.info("NEG 2 PASS — raised as expected: %s", e)
    if _confirm("NEG 3  status of bogus order id — expect '' not crash"):
        st = await osvc.get_order_status("SMOKE-BOGUS-3")
        log.info("NEG 3 %s — status=%r", "PASS" if st in ("",) else "note", st)


async def run_money_test(key: str, strike: int, ltp: float, qty: int) -> None:
    """Let a real SL stop actually TRIGGER, then prove exit_with_sl_cleanup
    detects the fill and SKIPS the sell (the naked-short guard)."""
    _state.update(pos_key=key, last_ltp=ltp)
    if not _confirm(f"MONEY  BUY {qty} × {strike} then SL trigger 5%% below — will take a real ~5%% loss"):
        return
    resp = await osvc.place_order(key, "BUY", qty, price=round(ltp * 1.10, 1), tag=_TAG)
    _state["entry_order_id"] = _order_id(resp)
    d = await _poll_terminal(_state["entry_order_id"], ("complete",), timeout_s=90.0)
    if (d.get("status") or "").lower() != "complete":
        raise RuntimeError(f"money-test entry did not fill: {d}")
    _state["entry_order_id"] = ""   # filled — nothing left for flatten to cancel
    _state["pos_qty"] = qty
    fill = float(d.get("average_price") or ltp)
    trig = round(fill * 0.95, 1)
    resp = await osvc.place_order(key, "SELL", qty, price=osvc.sl_limit_price(trig),
                                  order_type="SL", trigger_price=trig, tag=_TAG)
    _state["sl_order_id"] = _order_id(resp)
    log.info("SL %s trigger=%.1f limit=%.1f — waiting for it to FIRE (up to 15 min, Ctrl+C aborts+flattens)",
             _state["sl_order_id"], trig, osvc.sl_limit_price(trig))
    d = await _poll_terminal(_state["sl_order_id"], ("complete",), timeout_s=900)
    if (d.get("status") or "").lower() != "complete":
        log.warning("SL did not trigger in window — flattening manually")
        return
    _state["pos_qty"] = 0   # broker already sold via trigger
    outcome = await osvc.exit_with_sl_cleanup(key, qty, round(fill * 0.95, 1),
                                              sl_order_id=_state["sl_order_id"], tag=_TAG)
    _state["sl_order_id"] = ""
    log.info("NAKED-SHORT GUARD: exit_with_sl_cleanup → %r — %s",
             outcome, "PASS" if outcome == "skipped_sl_filled" else "FAIL — WOULD HAVE DOUBLE-SOLD")
    pos = await osvc.get_positions()
    open_qty = sum(abs(int(p.get("quantity") or 0)) for p in pos
                   if p.get("instrument_token") == key)
    log.info("POSITIONS after money test: qty=%d — %s",
             open_qty, "PASS (flat)" if open_qty == 0 else "FAIL — NOT FLAT, CLOSE MANUALLY")


def _install_dry_run() -> None:
    """Replace HTTP-touching functions with loggers so the full flow can be
    rehearsed without the market."""
    seq = {"n": 0}
    book: dict[str, dict] = {}   # stateful fake broker: oid → order record
    async def fake_place(token, side, qty, price=0.0, tag="uptox",
                         order_type="LIMIT", trigger_price=0.0):
        seq["n"] += 1
        oid = f"DRY{seq['n']}"
        book[oid] = {"status": "trigger pending" if order_type in ("SL", "SL-M") else "complete",
                     "average_price": price or 10.0, "trigger_price": trigger_price,
                     "price": price, "filled_quantity": qty}
        log.info("[dry] place %s %s %s qty=%d price=%.1f trig=%.1f → %s",
                 order_type, side, token, qty, price, trigger_price, oid)
        return {"data": {"order_ids": [oid]}}
    async def fake_details(oid):
        return dict(book.get(oid, {}))
    async def fake_modify(oid, qty, trig, order_type="SL", price=None):
        if oid not in book:
            raise RuntimeError("[dry] 404 order not found")
        book[oid]["trigger_price"] = trig
        book[oid]["price"] = price if price is not None else osvc.sl_limit_price(trig)
        log.info("[dry] modify %s trig=%.1f limit=%.1f", oid, trig, book[oid]["price"])
        return {}
    async def fake_cancel(oid):
        if oid not in book:
            raise RuntimeError("[dry] 404 order not found")
        book[oid]["status"] = "cancelled"
        log.info("[dry] cancel %s", oid)
        return {}
    async def fake_positions():
        return []
    osvc.place_order, osvc.get_order_details = fake_place, fake_details
    osvc.modify_order, osvc.cancel_order = fake_modify, fake_cancel
    osvc.get_positions = fake_positions


async def amain(args) -> None:
    now = datetime.now(_IST)
    if not args.dry_run:
        market_open = (9, 15) <= (now.hour, now.minute) < (15, 30)
        if not market_open and not args.force:
            raise SystemExit(
                "market closed (IST %s) — after-hours orders are treated as AMO and "
                "rejected (UDAPI1162). Run 10:00-15:00 IST, or --force to override."
                % now.strftime("%H:%M")
            )
        if not (10 <= now.hour < 15):
            log.warning("outside the calm 10:00-15:00 IST window — proceed only if intentional")
        if not args.expiry:
            expiries = await _list_expiries()
            if not expiries:
                raise RuntimeError("could not fetch valid expiries — check token")
            today = now.strftime("%Y-%m-%d")
            # prefer the nearest expiry that is NOT today (expiry-day gamma)
            args.expiry = next((e for e in expiries if e > today), expiries[0])
            log.info("auto-picked expiry %s (valid: %s)", args.expiry, expiries[:4])
        if args.expiry == now.strftime("%Y-%m-%d"):
            log.warning("selected expiry is TODAY — expiry-day gamma, premiums move fast")

    if args.dry_run:
        key, strike, ltp = "NSE_FO|DRYRUN", 23000, 10.0
    else:
        key, strike, ltp = await _pick_strike(args.expiry, args.side,
                                              args.min_prem, args.max_prem)
    if ltp > args.max_prem:
        raise RuntimeError(f"premium {ltp} above hard cap {args.max_prem} — abort")
    log.info("SELECTED: %s %s %s @ ₹%.2f  qty=%d  (max spend ₹%.0f)",
             strike, args.side, key, ltp, args.qty, ltp * args.qty * 1.05)
    if not _confirm("proceed with this contract"):
        return

    try:
        await run_happy_path(key, strike, ltp, args.qty)
        await run_negative_paths()
        if args.money_test:
            await run_money_test(key, strike, ltp, args.qty)
        log.info("SMOKE TEST COMPLETE — review PASS/FAIL lines above and the log file")
    except (KeyboardInterrupt, Exception) as e:
        if not isinstance(e, KeyboardInterrupt):
            log.error("SMOKE TEST ERROR: %s", e)
        log.warning("aborting — flattening script-owned exposure")
        await _flatten()
        raise SystemExit(1)


def main() -> None:
    p = argparse.ArgumentParser(description="Attended live-API smoke test (1 lot, deep OTM)")
    p.add_argument("--expiry",     type=str, default="",
                   help="option expiry YYYY-MM-DD (omit to auto-pick the nearest non-today expiry)")
    p.add_argument("--side",       choices=("CE", "PE"), default="PE")
    p.add_argument("--qty",        type=int, default=65, help="quantity = 1 lot (default 65)")
    p.add_argument("--min-prem",   type=float, default=5.0)
    p.add_argument("--max-prem",   type=float, default=15.0)
    p.add_argument("--money-test", action="store_true",
                   help="also let a real SL stop trigger to prove the naked-short guard")
    p.add_argument("--dry-run",    action="store_true", help="rehearse full flow, no HTTP")
    p.add_argument("--force",      action="store_true", help="override the market-hours guard")
    args = p.parse_args()

    logfile = _setup_logging()
    log.info("log file: %s", logfile)
    if args.dry_run:
        _install_dry_run()
        log.info("DRY RUN — no orders will reach the broker")
    asyncio.run(amain(args))


if __name__ == "__main__":
    main()
