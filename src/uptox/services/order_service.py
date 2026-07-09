"""
Live order placement via Upstox v2 REST API.
Only called when trading_mode.is_live() == True.

Entries/exits are intraday LIMIT orders; the protective stop is a single
SL-M (market-on-trigger) SELL order resting at the broker. The exchange
allows only ONE stop order per position, so the trailing SL never places a
second order — it MODIFIES the resting SL-M's trigger price upward as the
trail floor rises. App-side exits (target, stale, reversal) cancel the SL-M
first, then sell; if the cancel fails because the SL-M already filled, the
sell is skipped — the position is already flat.

instrument_token format: "NSE_FO|<exchange_token>" — same as instrument_key
from option_chain_service.
"""
from __future__ import annotations

import asyncio
import logging

import httpx

log = logging.getLogger(__name__)

# v3 HFT endpoints — lower latency than api.upstox.com
_ORDER_URL   = "https://api-hft.upstox.com/v3/order/place"
_MODIFY_URL  = "https://api-hft.upstox.com/v3/order/modify"
_CANCEL_URL  = "https://api-hft.upstox.com/v3/order/cancel"
_DETAILS_URL = "https://api.upstox.com/v2/order/details"

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


async def place_order(
    instrument_token: str,
    transaction_type: str,   # "BUY" | "SELL"
    qty: int,
    price: float = 0.0,
    tag: str = "uptox",
    order_type: str = "LIMIT",     # "LIMIT" | "MARKET" | "SL-M"
    trigger_price: float = 0.0,    # required for SL-M
) -> dict:
    """
    Place an intraday order via Upstox v3 API. Returns response dict.
    Raises on HTTP error or API-level error.

    LIMIT: price > 0, trigger_price = 0.
    SL-M:  trigger_price > 0, price = 0 (fills at market once triggered).
    """
    if order_type == "LIMIT" and price <= 0:
        raise ValueError(f"Limit order requires price > 0, got {price}")
    if order_type == "SL-M" and trigger_price <= 0:
        raise ValueError(f"SL-M order requires trigger_price > 0, got {trigger_price}")

    payload = {
        "quantity":           qty,
        "product":            "I",          # intraday margin
        "validity":           "DAY",
        "price":              round(price, 1) if order_type == "LIMIT" else 0,
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

    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.post(_ORDER_URL, json=payload, headers=_headers())

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
    order_type: str = "SL-M",
) -> dict:
    """Modify a resting order — used to trail the SL-M trigger upward."""
    payload = {
        "order_id":           order_id,
        "quantity":           qty,
        "validity":           "DAY",
        "price":              0,
        "order_type":         order_type,
        "disclosed_quantity": 0,
        "trigger_price":      round(trigger_price, 1),
    }
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.put(_MODIFY_URL, json=payload, headers=_headers())
    data = r.json()
    if r.status_code not in (200, 201):
        log.error("LIVE MODIFY FAILED: HTTP %d  %s  payload=%s", r.status_code, data, payload)
        r.raise_for_status()
    log.info("LIVE SL MODIFIED: order_id=%s  trigger → %.1f  qty=%d", order_id, trigger_price, qty)
    return data


async def cancel_order(order_id: str) -> dict:
    """Cancel a resting order. Raises if the order is no longer open (e.g. filled)."""
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.delete(_CANCEL_URL, params={"order_id": order_id}, headers=_headers())
    data = r.json()
    if r.status_code not in (200, 201):
        log.warning("LIVE CANCEL FAILED: HTTP %d  %s  order_id=%s", r.status_code, data, order_id)
        r.raise_for_status()
    log.info("LIVE ORDER CANCELLED: order_id=%s", order_id)
    return data


async def get_order_status(order_id: str) -> str:
    """Return the order's status string ("complete", "open", "cancelled", ...)
    or "" if the lookup fails."""
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(_DETAILS_URL, params={"order_id": order_id}, headers=_headers())
        return ((r.json().get("data") or {}).get("status") or "").lower()
    except Exception as exc:
        log.error("LIVE ORDER STATUS ERROR (%s): %s", order_id, exc)
        return ""


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
                order_type="SL-M", trigger_price=sl_trigger, tag="uptox-sl",
            )
            ids = (data.get("data") or {}).get("order_ids", [])
            if ids and on_sl_order_id:
                on_sl_order_id(ids[0])
        except Exception as exc:
            log.error("LIVE SL-M PLACE ERROR (%s trigger=%.1f): %s — position UNPROTECTED "
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


def schedule_exit(
    instrument_token: str,
    qty: int,
    price: float,
    sl_order_id: str = "",
    tag: str = "uptox",
) -> None:
    """
    App-side exit sequence: cancel the resting SL-M first, then SELL LIMIT.
    If the cancel fails because the SL-M already filled (broker stopped us out
    in the same moment), the SELL is skipped — selling again would open a
    naked short.
    """
    if not instrument_token or qty <= 0 or price <= 0:
        log.warning("schedule_exit: invalid params  token=%s qty=%d price=%.1f",
                    instrument_token, qty, price)
        return

    async def _run() -> None:
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
                    return
                log.warning(
                    "SL-M cancel failed (status=%s) — proceeding with SELL anyway",
                    status or "unknown",
                )
        try:
            await place_order(instrument_token, "SELL", qty, price, tag)
        except Exception as exc:
            log.error("LIVE EXIT ERROR (SELL %s qty=%d price=%.1f): %s",
                      instrument_token, qty, price, exc)

    _spawn(_run())
