"""Converts raw proto Feed → TickData and writes to feed_store."""
from uptox.api import market_feed_pb2 as proto
from uptox.services import feed_store, sma_service
from uptox.services.feed_store import TickData


async def on_tick(instrument_key: str, feed: proto.Feed) -> None:
    tick = TickData(instrument_key=instrument_key)

    if feed.HasField("ff"):
        ff = feed.ff
        src = ff.marketFF if ff.HasField("marketFF") else ff.indexFF

        ltpc = src.ltpc
        tick.ltp   = ltpc.ltp
        tick.ltt   = ltpc.ltt
        tick.ltq   = ltpc.ltq
        tick.close = ltpc.cp

        if ff.HasField("marketFF"):
            mff = ff.marketFF

            # depth
            if mff.marketLevel.bidAskQuote:
                tick.bids = [
                    {"price": q.bp, "qty": q.bq, "orders": q.bno}
                    for q in mff.marketLevel.bidAskQuote
                ]
                tick.asks = [
                    {"price": q.ap, "qty": q.aq, "orders": q.ano}
                    for q in mff.marketLevel.bidAskQuote
                ]

            # greeks
            g = mff.optionGreeks
            tick.delta = g.delta
            tick.gamma = g.gamma
            tick.theta = g.theta
            tick.vega  = g.vega
            tick.iv    = g.iv

            # extended
            e = mff.eFeedDetails
            tick.oi        = e.oi
            tick.change_oi = e.changeOi
            tick.tbq       = e.tbq
            tick.tsq       = e.tsq
            tick.mbp_buy   = e.mbpBuy
            tick.mbp_sell  = e.mbpSell
            tick.vtt       = e.vtt
            tick.atp       = e.atp

    elif feed.HasField("ltpc"):
        tick.ltp   = feed.ltpc.ltp
        tick.ltt   = feed.ltpc.ltt
        tick.ltq   = feed.ltpc.ltq
        tick.close = feed.ltpc.cp

    feed_store.update(instrument_key, tick)
    if tick.ltp:
        sma_service.update_realtime(instrument_key, tick.ltp)
