"""
Telegram inbound command handler — long-polls getUpdates and dispatches
/exit, /sl, /status, /pause, /resume. Runs as a background worker
(see uptox.ui.app._telegram_command_loop).

Security: only messages from the configured telegram_chat_id are honored.
Anyone else's message is logged and ignored — this bot can close real
positions, so an unchecked sender would be a live trading-control hole.
"""
from __future__ import annotations

import asyncio
import logging

import httpx

log = logging.getLogger(__name__)

_API = "https://api.telegram.org/bot{token}/{method}"
_POLL_TIMEOUT = 25   # seconds — Telegram long-poll; keep under our own read timeout
_offset: int = 0     # update_id cursor, so getUpdates doesn't redeliver old messages


def _get_token() -> str:
    from uptox.config.settings import settings
    return getattr(settings, "telegram_token", "")


def _allowed_chat_id() -> str:
    from uptox.config.settings import settings
    return str(getattr(settings, "telegram_chat_id", "")).strip()


async def _reply(chat_id: str, text: str) -> None:
    token = _get_token()
    if not token:
        return
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            await c.post(
                _API.format(token=token, method="sendMessage"),
                json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            )
    except Exception as e:
        log.warning("telegram reply failed: %s", e)


def _fmt_status() -> str:
    from uptox.services import trade_book, trailing_sl_service
    ot = trade_book.get_open_trade()
    if not ot:
        return "No open trade."
    st = trailing_sl_service.get_status()
    return (
        f"{ot['option']} {ot['strike']}  entry ₹{ot['entry_price']:.1f}\n"
        f"current ₹{ot.get('current_price', ot['entry_price']):.1f}  "
        f"pnl {ot.get('pnl_pts', 0):+.1f}pt (₹{ot.get('pnl_rs', 0):+.0f})\n"
        f"SL ₹{st['trail_sl']:.1f}  target ₹{ot.get('target', 0):.1f}  "
        f"phase {st['phase']}"
    )


def _fmt_health() -> str:
    from uptox.services import feed_store, tick_handler, trade_book, bot_control, capital_service
    from uptox.services.market_calendar import is_trade_window

    feed_ok = feed_store.get_status()
    opt_age = tick_handler.get_option_feed_age_secs()
    opt_str = "no ticks yet" if opt_age is None else (
        f"{opt_age:.0f}s ago" + ("  ⚠️ STALE" if opt_age > tick_handler._OPTION_STALE_SECS else "")
    )

    ds = trade_book.get_daily_stats()
    can_trade, block_reason = trade_book.can_trade_today()
    ot = trade_book.get_open_trade()

    # Count today's blocked-entry log rows (best-effort — file may not exist yet)
    blocked_today = 0
    try:
        from pathlib import Path
        from datetime import date
        import json as _json
        p = Path("logs/blocked_entries.jsonl")
        if p.exists():
            today_str = date.today().isoformat()
            for line in p.read_text(encoding="utf-8").splitlines():
                if line.startswith('{"ts": "' + today_str):
                    blocked_today += 1
    except Exception:
        pass

    lines = [
        f"Feed        : {feed_ok}",
        f"Option ticks: {opt_str}",
        f"Window      : {'open' if is_trade_window() else 'closed'}",
        f"Auto-entry  : {'PAUSED' if bot_control.is_paused() else 'active'}",
        f"Open trade  : {'yes — ' + ot['option'] + ' ' + str(ot['strike']) if ot else 'no'}",
        f"Trades today: {ds['trades_today']}  (blocked attempts: {blocked_today})",
        f"Can trade   : {'yes' if can_trade else 'no — ' + (block_reason or '')}",
        f"Capital     : ₹{capital_service.get_capital():,.0f}",
    ]
    return "\n".join(lines)


async def _handle_command(text: str, chat_id: str) -> None:
    from uptox.services import trade_book, trailing_sl_service, bot_control, feed_store, option_chain_service

    parts = text.strip().split()
    cmd = parts[0].lower().lstrip("/")

    if cmd == "status":
        await _reply(chat_id, _fmt_status())

    elif cmd == "health":
        await _reply(chat_id, _fmt_health())

    elif cmd in ("today", "day"):
        from uptox.services.telegram_service import build_daily_report
        await _reply(chat_id, build_daily_report())

    elif cmd == "week":
        from uptox.services.telegram_service import build_weekly_report
        await _reply(chat_id, build_weekly_report())

    elif cmd == "month":
        from uptox.services.telegram_service import build_monthly_report
        await _reply(chat_id, build_monthly_report())

    elif cmd == "exit":
        ot = trade_book.get_open_trade()
        if not ot:
            await _reply(chat_id, "No open trade to exit.")
            return
        opt_key = ot.get("option_key", "")
        live = feed_store.get(opt_key) if opt_key else None
        exit_p = live.ltp if live else 0.0
        if not exit_p:
            strike_k = option_chain_service.get_key(ot.get("strike", 0), ot.get("option", ""))
            live2 = feed_store.get(strike_k) if strike_k else None
            exit_p = live2.ltp if live2 else 0.0
        if not exit_p:
            exit_p = ot.get("current_price") or ot["entry_price"]
        closed = trade_book.close_trade(exit_p, "telegram_exit")
        if closed:
            await _reply(
                chat_id,
                f"Closed {closed['option']} {closed['strike']} @ ₹{exit_p:.1f}  "
                f"pnl {closed['pnl_pts']:+.1f}pt (₹{closed['pnl_rs']:+.0f})",
            )
        else:
            await _reply(chat_id, "Exit failed — no open trade found.")

    elif cmd == "sl":
        if len(parts) < 2:
            await _reply(chat_id, "Usage: /sl <price>  e.g. /sl 120.5")
            return
        try:
            price = float(parts[1])
        except ValueError:
            await _reply(chat_id, f"Not a number: {parts[1]}")
            return
        if trailing_sl_service.set_sl(price):
            await _reply(chat_id, f"SL moved to ₹{price:.1f}")
        else:
            st = trailing_sl_service.get_status()
            if not st["active"]:
                await _reply(chat_id, "No open trade — SL not set.")
            else:
                await _reply(
                    chat_id,
                    f"Rejected — ₹{price:.1f} would loosen the SL "
                    f"(current floor ₹{st['sl_floor']:.1f}, SL only tightens).",
                )

    elif cmd == "pause":
        bot_control.pause()
        await _reply(chat_id, "Paused — no new auto-entries. Open trade (if any) still managed. /resume to re-enable.")

    elif cmd == "resume":
        bot_control.resume()
        await _reply(chat_id, "Resumed — auto-entry active.")

    elif cmd == "help":
        await _reply(
            chat_id,
            "<b>Trade control</b>\n"
            "/status — open trade P&amp;L, SL, target\n"
            "/exit — close open trade now\n"
            "/sl &lt;price&gt; — tighten SL manually\n"
            "/pause — stop new auto-entries\n"
            "/resume — re-enable auto-entries\n\n"
            "<b>Reports</b>\n"
            "/today — today's stats\n"
            "/week — this ISO week's report\n"
            "/month — this month's report\n"
            "/health — feed status, ticks, blocked count, capital",
        )

    else:
        await _reply(chat_id, f"Unknown command: {cmd}. Try /help")


async def _poll_once(token: str) -> bool:
    """Returns True on a clean poll (no error), False on an error response —
    caller backs off before retrying on False so a persistent error (e.g. a
    webhook conflict that deleteWebhook didn't clear in time) doesn't hammer
    the API in a tight loop."""
    global _offset
    async with httpx.AsyncClient(timeout=_POLL_TIMEOUT + 10) as c:
        resp = await c.get(
            _API.format(token=token, method="getUpdates"),
            params={"offset": _offset, "timeout": _POLL_TIMEOUT},
        )
    if resp.status_code != 200:
        # 409 = a webhook is registered on this bot token, which blocks long-polling.
        # Auto-clear it — this bot is polling-only, no webhook should ever be set.
        if resp.status_code == 409:
            log.warning("getUpdates 409 (webhook conflict) — clearing webhook")
            async with httpx.AsyncClient(timeout=10) as c2:
                await c2.get(_API.format(token=token, method="deleteWebhook"))
        else:
            log.warning("getUpdates %s: %s", resp.status_code, resp.text[:200])
        return False
    updates = resp.json().get("result", [])
    allowed = _allowed_chat_id()
    for upd in updates:
        _offset = upd["update_id"] + 1
        msg = upd.get("message") or {}
        text = msg.get("text", "")
        chat_id = str(msg.get("chat", {}).get("id", ""))
        if not text.startswith("/"):
            continue
        if not allowed or chat_id != allowed:
            log.warning("telegram command from unauthorized chat_id=%s ignored: %s", chat_id, text)
            continue
        try:
            await _handle_command(text, chat_id)
        except Exception as e:
            log.exception("telegram command handling error: %s", e)
            await _reply(chat_id, f"Error handling command: {e}")
    return True


async def run_forever() -> None:
    """Long-poll loop — call as a background worker at app startup."""
    _warned_missing = False
    _started_log_done = False
    while True:
        token = _get_token()
        chat_id = _allowed_chat_id()
        if not token or not chat_id:
            if not _warned_missing:
                log.warning(
                    "telegram commands idle — token %s, chat_id %s (set both in .env)",
                    "set" if token else "MISSING", chat_id or "MISSING",
                )
                _warned_missing = True
            await asyncio.sleep(30)
            continue
        if _warned_missing:
            log.info("telegram commands active — token and chat_id now set")
            _warned_missing = False
        if not _started_log_done:
            log.info("telegram command poller started (chat_id=%s)", chat_id)
            _started_log_done = True
        try:
            ok = await _poll_once(token)
            if not ok:
                await asyncio.sleep(5)   # back off on error (e.g. 409) — don't hammer the API
        except Exception as e:
            log.warning("telegram poll error: %s", e)
            await asyncio.sleep(5)
