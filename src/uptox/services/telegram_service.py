"""
Telegram notification service.
Sends messages via Bot API using token + chat_id from settings.
All sends are fire-and-forget (non-blocking).
Each notify_* function checks notification_settings before sending —
configurable from the Settings UI tab.
"""
from __future__ import annotations
import asyncio
import logging
from datetime import date

log = logging.getLogger(__name__)

_BASE_URL = "https://api.telegram.org/bot{token}/sendMessage"
_sent_daily_report_date: date | None = None


def _get_config() -> tuple[str, str]:
    from uptox.config.settings import settings
    return getattr(settings, "telegram_token", ""), getattr(settings, "telegram_chat_id", "")


def _enabled(key: str) -> bool:
    from uptox.services import notification_settings
    return notification_settings.is_enabled(key)


async def _send_async(text: str) -> None:
    token, chat_id = _get_config()
    if not token or not chat_id:
        return
    try:
        import httpx
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(
                _BASE_URL.format(token=token),
                json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            )
        if r.status_code != 200:
            log.warning("Telegram HTTP %d: %s", r.status_code, r.text[:200])
    except Exception as e:
        log.error("Telegram send failed: %s", e)


def send(text: str) -> None:
    """Queue a message. Safe to call from sync or async context."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            asyncio.create_task(_send_async(text))
        else:
            loop.run_until_complete(_send_async(text))
    except Exception as e:
        log.error("Telegram dispatch: %s", e)


# ── Formatted messages ────────────────────────────────────────────────────────

def notify_trade_open(trade: dict) -> None:
    if not _enabled("trade_open"):
        return
    opt_icon = "🟢" if trade["option"] == "CE" else "🔴"
    send(
        f"{opt_icon} <b>TRADE OPEN</b>\n"
        f"Strike : {trade['option']} {trade['strike']}\n"
        f"Entry  : ₹{trade['entry_price']:.1f}\n"
        f"Lots   : {trade['lots']}  (₹{trade['invested']:,.0f})\n"
        f"SL     : ₹{trade['sl']:.1f}\n"
        f"Target : ₹{trade['target']:.1f}\n"
        f"Expiry : {trade['expiry']}\n"
        f"Score  : {trade.get('score', 0):+d}/5\n"
        f"Reason : {trade.get('entry_reason', '—')}"
    )


def notify_trade_close(trade: dict) -> None:
    if not _enabled("trade_close"):
        return
    pnl_pts = trade.get("pnl_pts", 0.0)
    pnl_rs  = trade.get("pnl_rs",  0.0)
    peak    = trade.get("peak_price")
    max_pts = trade.get("max_pnl_pts")
    icon    = "✅" if pnl_pts >= 0 else "❌"
    send(
        f"{icon} <b>TRADE CLOSED</b>  — {trade.get('outcome', '—')}\n"
        f"Strike : {trade['option']} {trade['strike']}\n"
        f"Entry  : ₹{trade['entry_price']:.1f}  →  Exit: ₹{trade.get('exit_price', 0):.1f}\n"
        f"P&amp;L   : {pnl_pts:+.1f} pts  ₹{pnl_rs:+,.0f}\n"
        + (f"Peak   : ₹{peak:.1f}  (max {max_pts:+.1f} pts)\n" if peak and max_pts else "")
        + f"Reason : {trade.get('exit_reason', '—')}"
    )


def notify_partial_close(trade: dict, closed_lots: int, exit_price: float, pnl_rs: float) -> None:
    if not _enabled("partial_exit"):
        return
    send(
        f"🔶 <b>PARTIAL EXIT</b>  {trade['option']} {trade['strike']}\n"
        f"Closed  : {closed_lots} lot(s) @ ₹{exit_price:.1f}\n"
        f"P&amp;L    : ₹{pnl_rs:+,.0f}\n"
        f"Remaining: {trade.get('lots', 0)} lot(s) — trailing with tight SL"
    )


def notify_daily_report() -> None:
    global _sent_daily_report_date
    today = date.today()
    if _sent_daily_report_date == today:
        return
    _sent_daily_report_date = today
    if not _enabled("daily_report"):
        return

    from uptox.services import trade_book, capital_service
    ds  = trade_book.get_daily_stats()
    cap = capital_service.get_capital()
    base = capital_service.get_base_capital()
    all_t = trade_book.get_all_trades()
    today_str = today.isoformat()
    today_trades = [t for t in all_t if t.get("date") == today_str]
    wins   = sum(1 for t in today_trades if t.get("pnl_pts", 0) > 0)
    losses = sum(1 for t in today_trades if t.get("pnl_pts", 0) <= 0)

    sign = "+" if ds["pnl_rs_today"] >= 0 else ""
    send(
        f"📊 <b>Daily Report — {today_str}</b>\n"
        f"Trades   : {ds['trades_today']}  ({wins}W / {losses}L)\n"
        f"Day P&amp;L  : {sign}₹{ds['pnl_rs_today']:,.0f}  ({ds['pnl_pct_today']:+.1f}%)\n"
        f"Capital  : ₹{cap:,.0f}  (base ₹{base:,.0f})\n"
        f"Consec L : {ds['consec_losses']}"
        + (f"\n⚠️ {ds['blocked_reason']}" if ds.get("blocked_reason") else "")
    )


def _period_summary(trades: list[dict]) -> dict:
    wins   = [t for t in trades if t.get("pnl_pts", 0) > 0]
    losses = [t for t in trades if t.get("pnl_pts", 0) <= 0]
    pnl_rs = sum(t.get("pnl_rs", 0) for t in trades)
    wr     = len(wins) / len(trades) * 100 if trades else 0.0
    best   = max(trades, key=lambda t: t.get("pnl_pts", 0)) if trades else None
    worst  = min(trades, key=lambda t: t.get("pnl_pts", 0)) if trades else None
    return {
        "total": len(trades), "wins": len(wins), "losses": len(losses),
        "pnl_rs": pnl_rs, "win_rate": wr, "best": best, "worst": worst,
    }


def notify_weekly_report() -> None:
    if not _enabled("weekly_report"):
        return
    from uptox.services import trade_book, capital_service
    today = date.today()
    iso_year, iso_week, _ = today.isocalendar()
    all_t = trade_book.get_all_trades()
    week_trades = [
        t for t in all_t
        if t.get("date") and date.fromisoformat(t["date"]).isocalendar()[:2] == (iso_year, iso_week)
    ]
    s = _period_summary(week_trades)
    sign = "+" if s["pnl_rs"] >= 0 else ""
    lines = [
        f"📅 <b>Weekly Report — ISO Week {iso_week}, {iso_year}</b>",
        f"Trades : {s['total']}  ({s['wins']}W / {s['losses']}L)  WR: {s['win_rate']:.0f}%",
        f"Week P&amp;L: {sign}₹{s['pnl_rs']:,.0f}",
        f"Capital : ₹{capital_service.get_capital():,.0f}",
    ]
    if s["best"]:
        lines.append(f"Best   : {s['best']['option']} {s['best']['strike']} +{s['best']['pnl_pts']:.1f}pts")
    if s["worst"] and s["worst"]["pnl_pts"] < 0:
        lines.append(f"Worst  : {s['worst']['option']} {s['worst']['strike']} {s['worst']['pnl_pts']:.1f}pts")
    send("\n".join(lines))


def notify_monthly_report() -> None:
    if not _enabled("monthly_report"):
        return
    from uptox.services import trade_book, capital_service
    today = date.today()
    all_t = trade_book.get_all_trades()
    month_trades = [
        t for t in all_t
        if t.get("date") and t["date"][:7] == today.strftime("%Y-%m")
    ]
    s = _period_summary(month_trades)
    sign = "+" if s["pnl_rs"] >= 0 else ""
    lines = [
        f"🗓️ <b>Monthly Report — {today.strftime('%B %Y')}</b>",
        f"Trades : {s['total']}  ({s['wins']}W / {s['losses']}L)  WR: {s['win_rate']:.0f}%",
        f"Month P&amp;L: {sign}₹{s['pnl_rs']:,.0f}",
        f"Capital : ₹{capital_service.get_capital():,.0f}  (base ₹{capital_service.get_base_capital():,.0f})",
    ]
    if s["best"]:
        lines.append(f"Best   : {s['best']['option']} {s['best']['strike']} +{s['best']['pnl_pts']:.1f}pts")
    if s["worst"] and s["worst"]["pnl_pts"] < 0:
        lines.append(f"Worst  : {s['worst']['option']} {s['worst']['strike']} {s['worst']['pnl_pts']:.1f}pts")
    send("\n".join(lines))


def notify_daily_limit_hit(reason: str) -> None:
    if not _enabled("daily_limit_hit"):
        return
    send(f"🚫 <b>DAILY LIMIT HIT</b>\n{reason}\nNo more trades today.")


def notify_trade_update(trade: dict, status: dict) -> None:
    """Periodic status ping while a trade is open. Interval set in notification_settings."""
    if not _enabled("trade_update"):
        return
    pnl_pts = trade.get("pnl_pts", 0.0)
    pnl_rs  = trade.get("pnl_rs",  0.0)
    icon    = "🟢" if pnl_pts >= 0 else "🔴"
    phase_names = {0: "Initial SL", 1: "Breakeven", 2: "25% locked", 3: "50% locked", 4: "Target+ (trailing)"}
    send(
        f"{icon} <b>TRADE UPDATE</b>  {trade['option']} {trade['strike']}\n"
        f"Entry  : ₹{trade['entry_price']:.1f}  →  Now: ₹{trade.get('current_price', 0):.1f}\n"
        f"P&amp;L   : {pnl_pts:+.1f} pts  ₹{pnl_rs:+,.0f}\n"
        f"Peak   : ₹{status.get('peak', 0):.1f}\n"
        f"Trail SL: ₹{status.get('trail_sl', 0):.1f}\n"
        f"Phase  : {phase_names.get(status.get('phase', 0), '—')}\n"
        f"Target : ₹{trade.get('target', 0):.1f}"
    )


def notify_feed_status(status: str, detail: str = "") -> None:
    if not _enabled("feed_status"):
        return
    icons = {"disconnected": "⚠️", "reconnecting": "🔄", "connected": "✅"}
    icon  = icons.get(status, "ℹ️")
    send(f"{icon} <b>FEED {status.upper()}</b>" + (f"\n{detail}" if detail else ""))


def notify_approaching_limit(pnl_pct: float, limit_pct: float) -> None:
    if not _enabled("approaching_limit"):
        return
    send(
        f"⚠️ <b>APPROACHING DAILY LOSS LIMIT</b>\n"
        f"Current: {pnl_pct:+.1f}%   Limit: −{limit_pct:.1f}%\n"
        f"Consider pausing manually if this continues."
    )


def notify_market_update(snapshot: dict) -> None:
    """Periodic market snapshot, sent regardless of open trade. Interval set in notification_settings."""
    if not _enabled("market_update"):
        return
    trend_icon = "📈" if snapshot["sma_score"] > 0 else "📉" if snapshot["sma_score"] < 0 else "➖"
    send(
        f"{trend_icon} <b>MARKET UPDATE</b>  {snapshot['time']}\n"
        f"Nifty  : {snapshot['ltp']:,.1f}\n"
        f"Trend  : {snapshot['trend']}  (SMA{snapshot['sma_score']:+d} Flow{snapshot['flow_score']:+d})\n"
        f"Regime : {snapshot['regime']}  ATR={snapshot['atr']:.0f}\n"
        f"PCR    : {snapshot['pcr']:.2f}\n"
        f"OI Bias: {snapshot['oi_bias']}\n"
        f"Trade  : {snapshot['trade_status']}"
    )
