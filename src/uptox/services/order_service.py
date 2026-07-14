"""
Live order placement via Upstox v2 REST API.
Only called when trading_mode.is_live() == True.

Entries/exits are intraday LIMIT orders; the protective stop is a single
SL (stop-limit) SELL order resting at the broker — trigger plus our own
limit band below it (NSE options disallow SL-M; brokers otherwise attach a
band we don't control). The exchange allows only ONE stop order per
position, so the trailing SL never places a second order — it MODIFIES the
resting stop's trigger (and band) upward as the trail floor rises.
App-side exits (target, stale, reversal) cancel the stop first, then sell;
if the cancel fails because the stop already filled, the sell is skipped —
the position is already flat.

instrument_token format: "NSE_FO|<exchange_token>" — same as instrument_key
from option_chain_service.
"""
from __future__ import annotations

import asyncio
import logging

import httpx

log = logging.getLogger(__name__)

# Order endpoints exist on two hosts: api-hft (lower latency, but requires a
# static IP registered in the Upstox developer app — else 403 UDAPI1154) and
# the standard api host (no IP restriction). Try HFT first, fall back to
# standard once and remember for the session.
_HFT_BASE    = "https://api-hft.upstox.com"
_STD_BASE    = "https://api.upstox.com"
_DETAILS_URL = "https://api.upstox.com/v2/order/details"
_hft_blocked = False


async def _order_request(method: str, path: str, *, json_body: dict | None = None,
                         params: dict | None = None) -> httpx.Response:
    """Order-API request with automatic HFT→standard fallback on the
    static-IP block (UDAPI1154)."""
    global _hft_blocked
    bases = [_STD_BASE] if _hft_blocked else [_HFT_BASE, _STD_BASE]
    last: httpx.Response | None = None
    for base in bases:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.request(method, base + path, json=json_body,
                                params=params, headers=_headers())
        if base == _HFT_BASE and r.status_code == 403 and "UDAPI1154" in r.text:
            log.warning(
                "HFT endpoint blocked (no static IP registered with Upstox) — "
                "falling back to standard endpoint for this session"
            )
            _hft_blocked = True
            last = r
            continue
        return r
    return last  # both failed — caller handles status

# Gap between entry BUY and placing the protective SL-M — a stop SELL sent
# before the BUY fills would be a fresh short if triggered.
_SL_PLACE_DELAY_SECS = 2.0


def _headers() -> dict:
    from uptox.config.settings import settings
    if not settings.access_token:
        raise RuntimeError("No access token — cannot place live order")
    return {
        "Authorization": f"Bearer {settings.access_token}",
        "Accept":        "application/json",
        "Content-Type":  "application/json",
    }


# Stop orders on NSE options cannot be market-on-trigger (SL-M) — the broker
# silently converts them to stop-LIMIT with a band we don't control (seen
# 2026-07-13: our SL-M trigger 7.1 became "SL lmt @ 7.35" in Upstox). Place
# explicit SL orders instead: trigger + our own limit band below it, wide
# enough to fill through a fast move but bounded by our choice.
_SL_LIMIT_BAND = 0.05


def sl_limit_price(trigger: float) -> float:
    """Limit price for a SELL stop: trigger minus the protection band."""
    return round(max(trigger * (1.0 - _SL_LIMIT_BAND), 0.1), 1)


async def place_order(
    instrument_token: str,
    transaction_type: str,   # "BUY" | "SELL"
    qty: int,
    price: float = 0.0,
    tag: str = "uptox",
    order_type: str = "LIMIT",     # "LIMIT" | "MARKET" | "SL" | "SL-M"
    trigger_price: float = 0.0,    # required for SL / SL-M
) -> dict:
    """
    Place an intraday order via Upstox v3 API. Returns response dict.
    Raises on HTTP error or API-level error.

    LIMIT: price > 0, trigger_price = 0.
    SL:    trigger_price > 0 AND price > 0 (stop-limit — use for option stops).
    SL-M:  trigger_price > 0, price = 0 (indexes/futures only; options get
           silently converted to SL by the broker).
    """
    if order_type == "LIMIT" and price <= 0:
        raise ValueError(f"Limit order requires price > 0, got {price}")
    if order_type == "SL-M" and trigger_price <= 0:
        raise ValueError(f"SL-M order requires trigger_price > 0, got {trigger_price}")
    if order_type == "SL" and (trigger_price <= 0 or price <= 0):
        raise ValueError(f"SL order requires trigger AND limit price, got t={trigger_price} p={price}")

    payload = {
        "quantity":           qty,
        "product":            "I",          # intraday margin
        "validity":           "DAY",
        "price":              round(price, 1) if order_type in ("LIMIT", "SL") else 0,
        "tag":                tag,
        "instrument_token":   instrument_token,
        "order_type":         order_type,
        "transaction_type":   transaction_type.upper(),
        "disclosed_quantity": 0,
        "trigger_price":      round(trigger_price, 1) if trigger_price else 0,
        "is_amo":             False,
        "slice":              True,         # auto-split if qty > exchange freeze limit
    }

    log.info(
        "LIVE ORDER → %s %s %s  qty=%d  price=%.1f  trigger=%.1f",
        order_type, transaction_type.upper(), instrument_token, qty, price, trigger_price,
    )

    r = await _order_request("POST", "/v3/order/place", json_body=payload)

    data = r.json()
    if r.status_code not in (200, 201):
        log.error(
            "LIVE ORDER FAILED: HTTP %d  %s  payload=%s",
            r.status_code, data, payload,
        )
        r.raise_for_status()

    # v3 returns data.order_ids (list), not data.order_id (string)
    order_ids = (data.get("data") or {}).get("order_ids", [])
    log.info(
        "LIVE ORDER ACCEPTED: order_ids=%s  %s %s %s  qty=%d",
        order_ids, order_type, transaction_type.upper(), instrument_token, qty,
    )
    return data


async def modify_order(
    order_id: str,
    qty: int,
    trigger_price: float,
    order_type: str = "SL",
    price: float | None = None,
) -> dict:
    """Modify a resting stop — trail the trigger upward, moving our limit
    band with it (price defaults to sl_limit_price(trigger))."""
    if price is None:
        price = sl_limit_price(trigger_price) if order_type == "SL" else 0.0
    payload = {
        "order_id":           order_id,
        "quantity":           qty,
        "validity":           "DAY",
        "price":              round(price, 1),
        "order_type":         order_type,
        "disclosed_quantity": 0,
        "trigger_price":      round(trigger_price, 1),
    }
    r = await _order_request("PUT", "/v3/order/modify", json_body=payload)
    data = r.json()
    if r.status_code not in (200, 201):
        log.error("LIVE MODIFY FAILED: HTTP %d  %s  payload=%s", r.status_code, data, payload)
        r.raise_for_status()
    log.info("LIVE SL MODIFIED: order_id=%s  trigger → %.1f  qty=%d", order_id, trigger_price, qty)
    return data


async def cancel_order(order_id: str) -> dict:
    """Cancel a resting order. Raises if the order is no longer open (e.g. filled)."""
    r = await _order_request("DELETE", "/v3/order/cancel", params={"order_id": order_id})
    data = r.json()
    if r.status_code not in (200, 201):
        log.warning("LIVE CANCEL FAILED: HTTP %d  %s  order_id=%s", r.status_code, data, order_id)
        r.raise_for_status()
    log.info("LIVE ORDER CANCELLED: order_id=%s", order_id)
    return data


async def get_order_details(order_id: str) -> dict:
    """Full order record from the broker (status, average_price, trigger_price,
    filled_quantity, ...). Returns {} if the lookup fails."""
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(_DETAILS_URL, params={"order_id": order_id}, headers=_headers())
        return (r.json().get("data") or {})
    except Exception as exc:
        log.error("LIVE ORDER DETAILS ERROR (%s): %s", order_id, exc)
        return {}


async def get_order_status(order_id: str) -> str:
    """Return the order's status string ("complete", "open", "cancelled", ...)
    or "" if the lookup fails."""
    return ((await get_order_details(order_id)).get("status") or "").lower()


async def get_charges(instrument_token: str, qty: int, price: float,
                      transaction_type: str, product: str = "I") -> float | None:
    """Broker's own fee calculation for one order (₹ total: brokerage + STT +
    stamp + transaction + GST...). Standard-API rate class. None on failure."""
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(
                "https://api.upstox.com/v2/charges/brokerage",
                params={"instrument_token": instrument_token, "quantity": qty,
                        "product": product, "transaction_type": transaction_type.upper(),
                        "price": price},
                headers=_headers(),
            )
        charges = ((r.json().get("data") or {}).get("charges") or {})
        total = charges.get("total")
        return float(total) if total is not None else None
    except Exception as exc:
        log.error("CHARGES LOOKUP ERROR (%s %s qty=%d): %s",
                  transaction_type, instrument_token, qty, exc)
        return None


async def get_funds() -> dict | None:
    """Broker's live equity funds/margin (available_margin, used_margin, ...).
    Note: Upstox funds service is DOWN 00:00-05:30 IST daily (maintenance) —
    callers must treat None as 'unknown', never as zero. Post 2025-07-19 the
    equity object carries combined equity+commodity funds."""
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(
                "https://api.upstox.com/v2/user/get-funds-and-margin",
                params={"segment": "SEC"},
                headers=_headers(),
            )
        return ((r.json().get("data") or {}).get("equity")) or None
    except Exception as exc:
        log.error("FUNDS LOOKUP ERROR: %s", exc)
        return None


async def get_positions() -> list[dict]:
    """Broker's live intraday positions — ground truth for reconciliation.
    Returns [] on failure."""
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(
                "https://api.upstox.com/v2/portfolio/short-term-positions",
                headers=_headers(),
            )
        return (r.json().get("data") or [])
    except Exception as exc:
        log.error("LIVE POSITIONS ERROR: %s", exc)
        return []


def _spawn(coro) -> None:
    """Run a fire-and-forget coroutine from a sync context inside an asyncio loop."""
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(coro)
    except RuntimeError:
        # No running loop (e.g., test/script context) — run synchronously
        asyncio.run(coro)


def schedule_order(
    instrument_token: str,
    transaction_type: str,
    qty: int,
    price: float = 0.0,
    tag: str = "uptox",
) -> None:
    """
    Fire-and-forget live limit order from a sync context.
    Errors are logged but do NOT crash the caller — paper trade record is always kept.
    """
    if not instrument_token or qty <= 0:
        log.warning("schedule_order: invalid params  token=%s  qty=%d", instrument_token, qty)
        return
    if price <= 0:
        log.error("schedule_order: price=%.1f — limit order requires price > 0, skipping", price)
        return

    async def _run() -> None:
        try:
            await place_order(instrument_token, transaction_type, qty, price, tag)
        except Exception as exc:
            log.error(
                "LIVE ORDER ERROR (%s %s qty=%d price=%.1f): %s",
                transaction_type, instrument_token, qty, price, exc,
            )

    _spawn(_run())


def schedule_entry_with_sl(
    instrument_token: str,
    qty: int,
    entry_price: float,
    sl_trigger: float,
    on_sl_order_id=None,   # callback(order_id: str) — invoked once the SL-M rests
) -> None:
    """
    Entry sequence: BUY LIMIT, short delay for the fill, then place the single
    protective SL-M SELL at sl_trigger. The SL order id is handed back via
    on_sl_order_id so the caller can track/modify/cancel it later.
    """
    if not instrument_token or qty <= 0 or entry_price <= 0:
        log.warning("schedule_entry_with_sl: invalid params  token=%s qty=%d price=%.1f",
                    instrument_token, qty, entry_price)
        return

    async def _run() -> None:
        try:
            await place_order(instrument_token, "BUY", qty, entry_price)
        except Exception as exc:
            log.error("LIVE ENTRY ERROR (BUY %s qty=%d): %s — SL-M not placed",
                      instrument_token, qty, exc)
            return
        if sl_trigger <= 0:
            return
        await asyncio.sleep(_SL_PLACE_DELAY_SECS)
        try:
            data = await place_order(
                instrument_token, "SELL", qty,
                price=sl_limit_price(sl_trigger),
                order_type="SL", trigger_price=sl_trigger, tag="uptox-sl",
            )
            ids = (data.get("data") or {}).get("order_ids", [])
            if ids and on_sl_order_id:
                on_sl_order_id(ids[0])
        except Exception as exc:
            log.error("LIVE SL PLACE ERROR (%s trigger=%.1f): %s — position UNPROTECTED "
                      "at broker, app-side trail still active",
                      instrument_token, sl_trigger, exc)

    _spawn(_run())


def schedule_sl_modify(order_id: str, qty: int, trigger_price: float) -> None:
    """Fire-and-forget trail update: move the resting SL-M's trigger up."""
    if not order_id or trigger_price <= 0:
        return

    async def _run() -> None:
        try:
            await modify_order(order_id, qty, trigger_price)
        except Exception as exc:
            log.error("LIVE SL MODIFY ERROR (%s trigger=%.1f): %s", order_id, trigger_price, exc)

    _spawn(_run())


async def exit_with_sl_cleanup(
    instrument_token: str,
    qty: int,
    price: float,
    sl_order_id: str = "",
    tag: str = "uptox",
) -> str:
    """
    App-side exit sequence: cancel the resting SL-M first, then SELL LIMIT.
    If the cancel fails because the SL-M already filled (broker stopped us out
    in the same moment), the SELL is skipped — selling again would open a
    naked short. Returns "skipped_sl_filled", "sold", or "error".
    """
    if sl_order_id:
        try:
            await cancel_order(sl_order_id)
        except Exception:
            status = await get_order_status(sl_order_id)
            if status == "complete":
                log.warning(
                    "Exit skipped: SL-M %s already FILLED at broker — position flat",
                    sl_order_id,
                )
                return "skipped_sl_filled"
            log.warning(
                "SL-M cancel failed (status=%s) — proceeding with SELL anyway",
                status or "unknown",
            )
    try:
        await place_order(instrument_token, "SELL", qty, price, tag)
        return "sold"
    except Exception as exc:
        log.error("LIVE EXIT ERROR (SELL %s qty=%d price=%.1f): %s",
                  instrument_token, qty, price, exc)
        return "error"


def schedule_exit(
    instrument_token: str,
    qty: int,
    price: float,
    sl_order_id: str = "",
    tag: str = "uptox",
) -> None:
    """Fire-and-forget wrapper around exit_with_sl_cleanup()."""
    if not instrument_token or qty <= 0 or price <= 0:
        log.warning("schedule_exit: invalid params  token=%s qty=%d price=%.1f",
                    instrument_token, qty, price)
        return
    _spawn(exit_with_sl_cleanup(instrument_token, qty, price, sl_order_id, tag))
