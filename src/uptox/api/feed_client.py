"""
Upstox WebSocket market data feed client.

Flow:
  1. GET /v3/feed/market-data-feed/authorize  → wss URL (one-time)
  2. Connect WSS → send subscribe message
  3. Receive protobuf frames → decode → emit via callback
"""
import json
import asyncio
import httpx
import websockets

from uptox.api import market_feed_pb2 as proto
from uptox.config.settings import settings
from uptox.services import feed_store

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


async def _get_wss_url(access_token: str) -> str:
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            AUTHORIZE_URL,
            headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        return data["data"]["authorized_redirect_uri"]


def _decode(raw: bytes) -> proto.FeedResponse:
    feed = proto.FeedResponse()
    feed.ParseFromString(raw)
    return feed


def _subscribe_msg(instrument_keys: list[str], mode: str = "full") -> str:
    return json.dumps({
        "guid": "uptox-feed-001",
        "method": "sub",
        "data": {"mode": mode, "instrumentKeys": instrument_keys},
    })


async def run_feed(
    on_tick,  # async callable(instrument_key: str, feed: proto.Feed)
    instrument_keys: list[str] | None = None,
    mode: str = "full",
    reconnect: bool = True,
    reconnect_delay: float = 5.0,
) -> None:
    """
    Connect to Upstox WSS and call on_tick for every incoming tick.
    Runs forever; set reconnect=False to exit on disconnect.
    """
    keys = instrument_keys or DEFAULT_INSTRUMENTS
    token = settings.access_token

    while True:
        try:
            wss_url = await _get_wss_url(token)
            print(f"[feed] Connecting: {wss_url[:60]}...")

            async with websockets.connect(
                wss_url,
                additional_headers={"Authorization": f"Bearer {token}"},
                ping_interval=30,
                ping_timeout=10,
            ) as ws:
                await ws.send(_subscribe_msg(keys, mode))
                if _extra_keys:
                    await ws.send(_subscribe_msg(_extra_keys, mode))
                feed_store.set_status("connected")
                print(f"[feed] Subscribed {len(keys) + len(_extra_keys)} instruments in '{mode}' mode")

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
                                print(f"[feed] Decode error: {e}")
                finally:
                    sender.cancel()

        except Exception as e:
            feed_store.set_status("disconnected")
            print(f"[feed] Disconnected: {e}")
            if not reconnect:
                break
            feed_store.set_status("reconnecting")
            print(f"[feed] Reconnecting in {reconnect_delay}s...")
            await asyncio.sleep(reconnect_delay)
