"""Converts raw proto Feed → TickData and writes to feed_store."""
import asyncio
from datetime import datetime, time as dt_time
from zoneinfo import ZoneInfo

from uptox.api import market_feed_pb2 as proto
from uptox.services import (
    feed_store, sma_service, option_chain_service,
    trailing_sl_service, trade_book,
)
from uptox.services.feed_store import TickData

_NIFTY  = "NSE_INDEX|Nifty 50"
_BANK   = "NSE_INDEX|Nifty Bank"
_IST    = ZoneInfo("Asia/Kolkata")

_REPORT_TIME       = dt_time(15, 30)
_report_sent_today: str = ""
_last_bar_slot:     int = -1   # minute // 5, used to detect 5-min bar close


async def on_tick(instrument_key: str, feed: proto.Feed) -> None:
    global _report_sent_today, _last_bar_slot

    tick = TickData(instrument_key=instrument_key)

    if feed.HasField("fullFeed"):
        ff  = feed.fullFeed
        src = ff.marketFF if ff.HasField("marketFF") else ff.indexFF

        ltpc       = src.ltpc
        tick.ltp   = ltpc.ltp
        tick.ltt   = ltpc.ltt
        tick.ltq   = ltpc.ltq
        tick.close = ltpc.cp

        if ff.HasField("marketFF"):
            mff = ff.marketFF
            if mff.marketLevel.bidAskQuote:
                tick.bids = [{"price": q.bidP, "qty": q.bidQ} for q in mff.marketLevel.bidAskQuote]
                tick.asks = [{"price": q.askP, "qty": q.askQ} for q in mff.marketLevel.bidAskQuote]
            g          = mff.optionGreeks
            tick.delta = g.delta
            tick.gamma = g.gamma
            tick.theta = g.theta
            tick.vega  = g.vega
            tick.iv    = mff.iv
            tick.oi    = mff.oi
            tick.tbq   = mff.tbq
            tick.tsq   = mff.tsq
            tick.vtt   = mff.vtt
            tick.atp   = mff.atp

    elif feed.HasField("ltpc"):
        tick.ltp   = feed.ltpc.ltp
        tick.ltt   = feed.ltpc.ltt
        tick.ltq   = feed.ltpc.ltq
        tick.close = feed.ltpc.cp

    feed_store.update(instrument_key, tick)

    if not tick.ltp:
        return

    sma_service.update_realtime(instrument_key, tick.ltp)

    # ── 5-min bar close detection (Nifty only) ────────────────────────────────
    if instrument_key == _NIFTY:
        now      = datetime.now(_IST)
        bar_slot = now.hour * 12 + now.minute // 5
        if bar_slot != _last_bar_slot and _last_bar_slot != -1:
            from uptox.services.data_recorder import record_bar
            record_bar()
            # Advance trailing SL phase on bar close — not per tick — to avoid noise shakeouts
            ot = trade_book.get_open_trade()
            if ot:
                opt_tick = feed_store.get(ot.get("option_key", ""))
                if opt_tick and opt_tick.ltp:
                    trailing_sl_service.on_bar_close(opt_tick.ltp)
        _last_bar_slot = bar_slot

    # ── IV rank update (ATM option ticks only) ────────────────────────────────
    if tick.iv and instrument_key not in (_NIFTY, _BANK):
        from uptox.services import iv_rank_service
        iv_rank_service.update(instrument_key, tick.iv)

    # ── ATM subscription refresh on Nifty move ────────────────────────────────
    if instrument_key == _NIFTY and option_chain_service.atm_changed(tick.ltp):
        keys = option_chain_service.get_keys_for_atm(tick.ltp, n=5)
        if keys:
            from uptox.api.feed_client import subscribe
            asyncio.create_task(subscribe(keys))

    # ── Open trade tick processing ────────────────────────────────────────────
    ot = trade_book.get_open_trade()
    if ot and ot.get("option_key") == instrument_key:
        if trade_book.should_session_close():
            _close_and_notify(tick.ltp, "session_close_3:25PM")
            return

        target_just_crossed = trade_book.update_open_pnl(tick.ltp)
        closed = False

        if target_just_crossed:
            action = trade_book.get_target_action(tick.ltp)
            if action == "close":
                _close_and_notify(tick.ltp, "target_volatile")
                closed = True
            elif action == "partial":
                # Partial exit: close half, trail rest
                _partial_exit_and_notify(tick.ltp)
                closed = True   # open trade mutated, skip trail check this tick

        if not closed and trailing_sl_service.update(instrument_key, tick.ltp):
            _close_and_notify(tick.ltp, "trail_sl")

    # ── Daily report + learning at 3:30 PM (once) ────────────────────────────
    now   = datetime.now(_IST)
    today = now.strftime("%Y-%m-%d")
    if now.time() >= _REPORT_TIME and _report_sent_today != today:
        _report_sent_today = today
        from uptox.services.telegram_service import notify_daily_report
        from uptox.services import learning_service
        notify_daily_report()
        asyncio.create_task(asyncio.to_thread(learning_service.run))


def _close_and_notify(price: float, reason: str) -> None:
    closed = trade_book.close_trade(price, reason)
    if closed:
        from uptox.services.telegram_service import notify_trade_close
        notify_trade_close(closed)


def _partial_exit_and_notify(price: float) -> None:
    result = trade_book.partial_close_trade(price, "partial_target")
    if result:
        from uptox.services.telegram_service import notify_partial_close
        notify_partial_close(result["remaining_trade"], result["closed_lots"], price, result["pnl_rs"])
