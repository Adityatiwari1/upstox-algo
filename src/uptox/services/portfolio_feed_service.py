"""
Upstox Portfolio Stream Feed — WebSocket for live order updates.

Flow:
  1. GET /v2/feed/portfolio-stream-feed/authorize?update_types=order  → wss URL
  2. Connect WSS → receive JSON order update messages
  3. Store latest order states in memory for dashboard display
  4. Telegram notification on fill/rejection

Only meaningful when trading_mode.is_live() — in paper mode the feed still
connects but there are no Upstox orders to track.
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections import deque
from datetime import datetime, time as dt_time
from zoneinfo import ZoneInfo

import httpx
import websockets

_log = logging.getLogger(__name__)

_AUTHORIZE_URL = "https://api.upstox.com/v2/feed/portfolio-stream-feed/authorize"

_IST        = ZoneInfo("Asia/Kolkata")
_FEED_START = dt_time(9, 0)
_FEED_END   = dt_time(15, 35)

# order_id → latest update dict (most recent 50 orders kept)
_order_states: dict[str, dict] = {}
_recent: deque[dict] = deque(maxlen=50)

# Connection health
_status: str = "disconnected"   # "connected" | "disconnected" | "reconnecting"


# ── public getters ────────────────────────────────────────────────────────────

def get_status() -> str:
    return _status


def get_order(order_id: str) -> dict | None:
    return _order_states.get(order_id)


def get_recent_orders(n: int = 10) -> list[dict]:
    """Most recent n order updates, newest first."""
    items = list(_recent)
    return list(reversed(items))[:n]


def get_last_order() -> dict | None:
    return _recent[-1] if _recent else None


# ── internal helpers ──────────────────────────────────────────────────────────

def _on_order_update(msg: dict) -> None:
    global _order_states
    order_id = msg.get("order_id") or msg.get("orderId")
    if not order_id:
        return

    _order_states[order_id] = msg
    _recent.append(msg)

    status   = (msg.get("status") or "").lower()
    sym      = msg.get("trading_symbol") or msg.get("tradingsymbol") or msg.get("instrument_token", "?")
    qty      = msg.get("quantity", 0)
    txn      = msg.get("transaction_type", "?")
    avg      = msg.get("average_price", 0)
    filled   = msg.get("filled_quantity", 0)
    err_msg  = msg.get("status_message", "")

    _log.info(
        "ORDER UPDATE [%s]: %s  %s  qty=%s  filled=%s  avg=%.2f  status=%s  %s",
        order_id, txn, sym, qty, filled, avg, status, err_msg or "",
    )

    # Notify on terminal states
    if status in ("complete", "filled"):
        _log.info("ORDER FILLED: %s %s %s qty=%s @ avg=%.2f", order_id, txn, sym, qty, avg)
        _notify_order(order_id, "FILLED", msg)
    elif status in ("rejected", "error"):
        _log.error("ORDER REJECTED: %s  reason=%s", order_id, err_msg)
        _notify_order(order_id, "REJECTED", msg)
    elif status == "cancelled":
        _log.warning("ORDER CANCELLED: %s", order_id)


def _notify_order(order_id: str, event: str, msg: dict) -> None:
    try:
        from uptox.services.telegram_service import send_message
        sym  = msg.get("trading_symbol") or msg.get("instrument_token", "?")
        txn  = msg.get("transaction_type", "?")
        qty  = msg.get("quantity", 0)
        avg  = msg.get("average_price", 0.0)
        err  = msg.get("status_message", "")
        text = (
            f"🔔 LIVE ORDER {event}\n"
            f"  {txn} {sym}  qty={qty}\n"
            + (f"  avg=₹{avg:.2f}\n" if avg else "")
            + (f"  reason: {err}\n" if err else "")
            + f"  order_id={order_id}"
        )
        send_message(text)
    except Exception as e:
        _log.warning("portfolio_feed notify error: %s", e)


async def _get_wss_url(access_token: str) -> str:
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.get(
            _AUTHORIZE_URL,
            params={"update_types": "order"},
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept":        "application/json",
            },
        )
        r.raise_for_status()
        return r.json()["data"]["authorized_redirect_uri"]


# ── main feed coroutine ───────────────────────────────────────────────────────

async def run_portfolio_feed() -> None:
    """
    Connect to portfolio stream and process order updates.
    Auto-reconnects with exponential backoff.
    Call once from app startup (runs forever).
    """
    global _status
    from uptox.config.settings import settings

    delay = 2.0

    while True:
        # Wait until market window before connecting
        now = datetime.now(_IST)
        t   = now.time()
        if not (_FEED_START <= t <= _FEED_END):
            await asyncio.sleep(30)
            continue

        if not settings.access_token:
            await asyncio.sleep(10)
            continue

        try:
            wss_url = await _get_wss_url(settings.access_token)
            _status = "connected"
            delay   = 2.0
            _log.info("[portfolio-feed] Connected")

            async with websockets.connect(
                wss_url,
                ping_interval=30,
                ping_timeout=10,
                additional_headers={
                    "Authorization": f"Bearer {settings.access_token}",
                    "Accept":        "*/*",
                },
            ) as ws:
                async for raw in ws:
                    try:
                        msg = json.loads(raw) if isinstance(raw, (str, bytes)) else {}
                        update_type = msg.get("update_type", "")
                        if update_type == "order":
                            _on_order_update(msg)
                    except Exception as e:
                        _log.debug("[portfolio-feed] parse error: %s", e)

        except Exception as e:
            _status = "reconnecting"
            _log.warning("[portfolio-feed] Disconnected: %s — retry in %.0fs", e, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60.0)
