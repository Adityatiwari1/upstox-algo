"""
Live order placement via Upstox v2 REST API.
Only called when trading_mode.is_live() == True.

All orders are intraday market orders (product="I", order_type="MARKET").
instrument_token format: "NSE_FO|<exchange_token>" — same as instrument_key
from option_chain_service.
"""
from __future__ import annotations

import asyncio
import logging

import httpx

log = logging.getLogger(__name__)

# v3 HFT endpoint — lower latency than api.upstox.com
_ORDER_URL = "https://api-hft.upstox.com/v3/order/place"


async def place_order(
    instrument_token: str,
    transaction_type: str,   # "BUY" | "SELL"
    qty: int,
    price: float = 0.0,
    tag: str = "uptox",
) -> dict:
    """
    Place an intraday LIMIT order via Upstox v3 API. Returns response dict.
    Raises on HTTP error or API-level error.

    instrument_token: full instrument key, e.g. "NSE_FO|57960"
    transaction_type: "BUY" to enter, "SELL" to exit
    qty: total quantity (lots × lot_size)
    price: limit price — must be > 0 for a limit order
    """
    from uptox.config.settings import settings

    if not settings.access_token:
        raise RuntimeError("No access token — cannot place live order")

    if price <= 0:
        raise ValueError(f"Limit order requires price > 0, got {price}")

    payload = {
        "quantity":           qty,
        "product":            "I",          # intraday margin
        "validity":           "DAY",
        "price":              round(price, 1),
        "tag":                tag,
        "instrument_token":   instrument_token,
        "order_type":         "LIMIT",
        "transaction_type":   transaction_type.upper(),
        "disclosed_quantity": 0,
        "trigger_price":      0,
        "is_amo":             False,
        "slice":              True,         # auto-split if qty > exchange freeze limit
    }

    log.info(
        "LIVE ORDER → %s %s  qty=%d  price=%.1f",
        transaction_type.upper(), instrument_token, qty, price,
    )

    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.post(
            _ORDER_URL,
            json=payload,
            headers={
                "Authorization": f"Bearer {settings.access_token}",
                "Accept":        "application/json",
                "Content-Type":  "application/json",
            },
        )

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
        "LIVE ORDER ACCEPTED: order_ids=%s  %s %s  qty=%d",
        order_ids, transaction_type.upper(), instrument_token, qty,
    )
    return data


def schedule_order(
    instrument_token: str,
    transaction_type: str,
    qty: int,
    price: float = 0.0,
    tag: str = "uptox",
) -> None:
    """
    Fire-and-forget live limit order from a sync context running inside an asyncio loop.
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

    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_run())
    except RuntimeError:
        # No running loop (e.g., test/script context) — run synchronously
        asyncio.run(_run())
