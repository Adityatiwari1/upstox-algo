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
_bar_closes_seen:   int = 0    # counts bar closes — every 3rd = 15 min trade update
_limit_warned_today: str = ""  # date string — approaching-limit alert sent once/day


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
            global _bar_closes_seen, _limit_warned_today
            from uptox.services.data_recorder import record_bar
            record_bar()
            # Advance trailing SL phase on bar close — not per tick — to avoid noise shakeouts
            ot = trade_book.get_open_trade()
            if ot:
                opt_tick = feed_store.get(ot.get("option_key", ""))
                if opt_tick and opt_tick.ltp:
                    from uptox.services.atr_service import get_atr
                    from uptox.services.sma_service import TimeFrame
                    trailing_sl_service.on_bar_close(
                        opt_tick.ltp,
                        atr_pts=get_atr(_NIFTY, TimeFrame.M5),
                    )

            _bar_closes_seen += 1

            # Every 3rd bar close (15 min): trade status ping, if a trade is open
            if _bar_closes_seen % 3 == 0 and ot:
                from uptox.services.telegram_service import notify_trade_update
                notify_trade_update(ot, trailing_sl_service.get_status())

            # Every 6th bar close (30 min): market snapshot, regardless of trade state
            if _bar_closes_seen % 6 == 0:
                _send_market_update(tick.ltp, ot)

            # Approaching daily loss limit — warn once per day at 75% of the limit
            from uptox.services import capital_service
            today_str = now.strftime("%Y-%m-%d")
            ds = trade_book.get_daily_stats()
            loss_limit = capital_service.get_daily_loss_limit_pct()
            if (_limit_warned_today != today_str
                    and ds["pnl_pct_today"] <= -0.75 * loss_limit):
                _limit_warned_today = today_str
                from uptox.services.telegram_service import notify_approaching_limit
                notify_approaching_limit(ds["pnl_pct_today"], loss_limit)
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


def _send_market_update(ltp: float, open_trade: dict | None) -> None:
    """Build and send a 30-min market snapshot — sent regardless of open trade."""
    from uptox.services import sma_service, option_chain_service, capital_service, feed_store
    from uptox.services.sma_service import TimeFrame
    from uptox.services.atr_service import get_regime
    from uptox.services.telegram_service import notify_market_update

    if not ltp:
        return

    smas = sma_service.get_smas(_NIFTY, TimeFrame.M5)
    s20, s50 = smas.get(20), smas.get(50)
    sma_score = (1 if ltp > s20 else -1 if s20 else 0) + (1 if ltp > s50 else -1 if s50 else 0)

    atm = round(ltp / 50) * 50
    ce_key = option_chain_service.get_key(atm, "CE")
    pe_key = option_chain_service.get_key(atm, "PE")
    ce_buy, ce_sell = feed_store.flow_qty(feed_store.get(ce_key) if ce_key else None)
    pe_buy, pe_sell = feed_store.flow_qty(feed_store.get(pe_key) if pe_key else None)
    flow_score = 1 if (ce_buy + pe_buy) > (ce_sell + pe_sell) else -1

    regime = get_regime()
    pcr = option_chain_service.get_pcr() or 0.0
    trend = "BULLISH" if sma_score > 0 else "BEARISH" if sma_score < 0 else "NEUTRAL"

    if open_trade:
        trade_status = f"OPEN — {open_trade['option']} {open_trade['strike']} ({open_trade.get('pnl_pts', 0):+.1f}pts)"
    else:
        trade_status = "No open trade"

    notify_market_update({
        "time":         datetime.now(_IST).strftime("%H:%M"),
        "ltp":          ltp,
        "trend":        trend,
        "sma_score":    sma_score,
        "flow_score":   flow_score,
        "regime":       regime["regime"],
        "atr":          regime["atr"],
        "pcr":          pcr,
        "oi_bias":      option_chain_service.get_oi_signal(ltp),
        "trade_status": trade_status,
    })


def _partial_exit_and_notify(price: float) -> None:
    result = trade_book.partial_close_trade(price, "partial_target")
    if result:
        from uptox.services.telegram_service import notify_partial_close
        notify_partial_close(result["remaining_trade"], result["closed_lots"], price, result["pnl_rs"])
