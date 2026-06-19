"""
Upstox WebSocket market data feed client.

Flow:
  1. GET /v3/feed/market-data-feed/authorize  → wss URL (one-time)
  2. Connect WSS → send subscribe message
  3. Receive protobuf frames → decode → emit via callback
"""
import json
import asyncio
import logging
import httpx
import websockets
from datetime import datetime, time as dt_time, timedelta
from zoneinfo import ZoneInfo

from uptox.api import market_feed_pb2 as proto
from uptox.config.settings import settings
from uptox.services import feed_store

_IST            = ZoneInfo("Asia/Kolkata")
_FEED_START     = dt_time(9,  0)
_FEED_END       = dt_time(15, 35)


async def _wait_for_market_open() -> None:
    """
    If called outside 09:00–15:35 IST on a trading day, sleep until next
    09:00 IST open. Sets status to 'market_closed' while waiting.
    """
    from uptox.services.market_calendar import is_trading_day
    while True:
        now  = datetime.now(_IST)
        t    = now.time()
        today = now.date()

        if is_trading_day(today) and _FEED_START <= t <= _FEED_END:
            return  # we're in the window — proceed

        # Calculate seconds until next 09:00 on a trading day
        if t < _FEED_START and is_trading_day(today):
            # Same day, wait until 09:00
            target = datetime.combine(today, _FEED_START, tzinfo=_IST)
        else:
            # Past close or weekend/holiday — find next trading day 09:00
            candidate = today + timedelta(days=1)
            while not is_trading_day(candidate):
                candidate += timedelta(days=1)
            target = datetime.combine(candidate, _FEED_START, tzinfo=_IST)

        wait_secs = (target - now).total_seconds()
        feed_store.set_status("market_closed")
        _log.info("[feed] Market closed — sleeping %.0fs until %s", wait_secs, target.strftime("%Y-%m-%d %H:%M IST"))
        # Sleep in 60s chunks so status stays fresh and can be interrupted
        while wait_secs > 0:
            await asyncio.sleep(min(60, wait_secs))
            wait_secs -= 60

_log = logging.getLogger(__name__)

AUTHORIZE_URL = "https://api.upstox.com/v3/feed/market-data-feed/authorize"

DEFAULT_INSTRUMENTS = [
    "NSE_INDEX|Nifty 50",
    "NSE_INDEX|Nifty Bank",
]

# Queue for dynamically sending subscribe messages to the live WSS connection.
# put() from any coroutine; run_feed's sender task drains it.
_send_queue: asyncio.Queue = asyncio.Queue()
# Option keys subscribed dynamically (so we can re-subscribe on reconnect)
_extra_keys: list[str] = []


async def subscribe(instrument_keys: list[str], mode: str = "full") -> None:
    """Queue additional instruments to subscribe on the active WSS connection."""
    global _extra_keys
    _extra_keys = list(instrument_keys)
    await _send_queue.put(_subscribe_msg(instrument_keys, mode))


async def _reauth() -> str:
    """Re-run headless login, persist new token, return it."""
    from uptox.api.auth import fetch_access_token
    from uptox.services.token_service import save_access_token
    _log.warning("[feed] Token expired — re-authenticating...")
    loop = asyncio.get_event_loop()
    new_token = await loop.run_in_executor(None, fetch_access_token)
    save_access_token(new_token)
    settings.access_token = new_token
    _log.info("[feed] Re-authentication successful.")
    return new_token


async def _get_wss_url(access_token: str) -> str:
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            AUTHORIZE_URL,
            headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
            timeout=10,
        )
        if resp.status_code == 401:
            raise _TokenExpiredError()
        resp.raise_for_status()
        data = resp.json()
        return data["data"]["authorized_redirect_uri"]


class _TokenExpiredError(Exception):
    pass


def _decode(raw: bytes) -> proto.FeedResponse:
    feed = proto.FeedResponse()
    feed.ParseFromString(raw)
    return feed


def _subscribe_msg(instrument_keys: list[str], mode: str = "full") -> bytes:
    return json.dumps({
        "guid": "uptox-feed-001",
        "method": "sub",
        "data": {"mode": mode, "instrumentKeys": instrument_keys},
    }).encode("utf-8")


async def run_feed(
    on_tick,
    instrument_keys: list[str] | None = None,
    mode: str = "full",
    reconnect: bool = True,
) -> None:
    """
    Connect to Upstox WSS and call on_tick for every tick.
    Auto-reconnects with exponential backoff (1s → 2 → 4 → 8 → 16 → 60s max).
    """
    keys  = instrument_keys or DEFAULT_INSTRUMENTS
    token = settings.access_token
    delay = 1.0   # initial backoff

    while True:
        await _wait_for_market_open()
        try:
            wss_url = await _get_wss_url(token)

            async with websockets.connect(
                wss_url,
                ping_interval=30,
                ping_timeout=10,
            ) as ws:
                await ws.send(_subscribe_msg(keys, mode))
                if _extra_keys:
                    await ws.send(_subscribe_msg(_extra_keys, mode))
                feed_store.set_status("connected")
                delay = 1.0   # reset backoff on successful connect
                _log.info("[feed] Connected — %d instruments in '%s' mode",
                          len(keys) + len(_extra_keys), mode)

                async def _sender() -> None:
                    while True:
                        msg = await _send_queue.get()
                        try:
                            await ws.send(msg)
                        finally:
                            _send_queue.task_done()

                sender = asyncio.ensure_future(_sender())
                try:
                    async for raw in ws:
                        if isinstance(raw, bytes):
                            try:
                                response = _decode(raw)
                                for key, feed in response.feeds.items():
                                    await on_tick(key, feed)
                            except Exception as e:
                                _log.error("[feed] Decode error: %s", e, exc_info=True)
                finally:
                    sender.cancel()

        except _TokenExpiredError:
            feed_store.set_status("reconnecting")
            try:
                token = await _reauth()
                delay = 1.0
            except Exception as auth_err:
                _log.error("[feed] Re-auth failed: %s", auth_err, exc_info=True)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60.0)
            continue

        except Exception as e:
            feed_store.set_status("disconnected")
            _log.error("[feed] Disconnected: %s", e, exc_info=True)
            if not reconnect:
                break
            feed_store.set_status("reconnecting")
            _log.info("[feed] Reconnecting in %.0fs...", delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60.0)
