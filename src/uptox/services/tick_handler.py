"""Converts raw proto Feed → TickData and writes to feed_store."""
import asyncio
from uptox.api import market_feed_pb2 as proto
from uptox.services import (
    feed_store, sma_service, option_chain_service,
    trailing_sl_service, trade_book,
)
from uptox.services.feed_store import TickData

_NIFTY = "NSE_INDEX|Nifty 50"


async def on_tick(instrument_key: str, feed: proto.Feed) -> None:
    tick = TickData(instrument_key=instrument_key)

    if feed.HasField("fullFeed"):
        ff = feed.fullFeed
        src = ff.marketFF if ff.HasField("marketFF") else ff.indexFF

        ltpc = src.ltpc
        tick.ltp   = ltpc.ltp
        tick.ltt   = ltpc.ltt
        tick.ltq   = ltpc.ltq
        tick.close = ltpc.cp

        if ff.HasField("marketFF"):
            mff = ff.marketFF

            # depth (v3: bidP/bidQ/askP/askQ, no orders count)
            if mff.marketLevel.bidAskQuote:
                tick.bids = [
                    {"price": q.bidP, "qty": q.bidQ}
                    for q in mff.marketLevel.bidAskQuote
                ]
                tick.asks = [
                    {"price": q.askP, "qty": q.askQ}
                    for q in mff.marketLevel.bidAskQuote
                ]

            # greeks (v3: iv is on mff, not optionGreeks)
            g = mff.optionGreeks
            tick.delta = g.delta
            tick.gamma = g.gamma
            tick.theta = g.theta
            tick.vega  = g.vega
            tick.iv    = mff.iv

            # extended (v3: flat fields on mff, no eFeedDetails)
            tick.oi  = mff.oi
            tick.tbq = mff.tbq
            tick.tsq = mff.tsq
            tick.vtt = mff.vtt
            tick.atp = mff.atp

    elif feed.HasField("ltpc"):
        tick.ltp   = feed.ltpc.ltp
        tick.ltt   = feed.ltpc.ltt
        tick.ltq   = feed.ltpc.ltq
        tick.close = feed.ltpc.cp

    feed_store.update(instrument_key, tick)
    if tick.ltp:
        sma_service.update_realtime(instrument_key, tick.ltp)
        if instrument_key == _NIFTY and option_chain_service.atm_changed(tick.ltp):
            keys = option_chain_service.get_keys_for_atm(tick.ltp, n=5)
            if keys:
                from uptox.api.feed_client import subscribe
                asyncio.create_task(subscribe(keys))
        # Per-tick updates for open paper trade
        ot = trade_book.get_open_trade()
        if ot and ot.get("option_key") == instrument_key:
            # Hero/Zero: force exit at 3:29 PM before settlement
            if trade_book.should_force_close():
                trade_book.close_trade(tick.ltp, "expiry_force_close")
                return
            target_just_crossed = trade_book.update_open_pnl(tick.ltp)
            closed = False
            if target_just_crossed:
                action = trade_book.get_target_action(tick.ltp)
                if action == "close":
                    trade_book.close_trade(tick.ltp, "target_volatile")
                    closed = True
            # Trail SL rises with price; sole exit in trending market
            if not closed and trailing_sl_service.update(instrument_key, tick.ltp):
                trade_book.close_trade(tick.ltp, "trail_sl")
