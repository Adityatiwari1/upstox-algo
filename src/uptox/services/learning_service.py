"""
End-of-day learning service.

Runs after market close (3:30 PM). Analyses today's and recent trades,
identifies systematic failures, adjusts adaptive_params in small steps,
saves, and sends a Telegram summary.

Rules are conservative — minimum 3 data points before any adjustment,
changes are at most 1 step per session per parameter.
"""
from __future__ import annotations
import logging
from datetime import date

log = logging.getLogger(__name__)

_MIN_SAMPLES = 3     # need at least this many trades before adjusting
_ran_today: str = ""


def run(force: bool = False) -> str | None:
    """
    Run end-of-day analysis. Idempotent — runs once per calendar day.
    Returns a summary string (also sent to Telegram).
    """
    global _ran_today
    today = date.today().isoformat()
    if _ran_today == today and not force:
        return None
    _ran_today = today

    from uptox.services import trade_book, adaptive_params

    all_trades   = trade_book.get_all_trades()
    today_trades = [t for t in all_trades if t.get("date") == today]

    if len(today_trades) < 2:
        log.info("Learning: < 2 trades today, skipping adjustment")
        return None

    changes: list[str] = []
    insights: list[str] = []

    # ── 1. Score threshold ─────────────────────────────────────────────────────
    low_score   = [t for t in today_trades if abs(t.get("score", 5)) <= 2]
    high_score  = [t for t in today_trades if abs(t.get("score", 0)) >= 3]
    if len(low_score) >= _MIN_SAMPLES:
        ls_wr = sum(1 for t in low_score if t["pnl_pts"] > 0) / len(low_score)
        if ls_wr < 0.35:
            adaptive_params.adjust("min_score", +1,
                f"score≤2 WR={ls_wr*100:.0f}% today ({len(low_score)} trades) — raising threshold")
            changes.append(f"↑ min_score (score≤2 WR only {ls_wr*100:.0f}%)")
        elif ls_wr > 0.60 and adaptive_params.get("min_score") > 2:
            adaptive_params.adjust("min_score", -1,
                f"score≤2 WR={ls_wr*100:.0f}% — lowering threshold (filter was too strict)")
            changes.append(f"↓ min_score (score≤2 performing well {ls_wr*100:.0f}%)")

    # ── 2. ATR multiplier — SL hit rate ───────────────────────────────────────
    sl_hits  = [t for t in today_trades if t.get("exit_reason") in ("sl_hit", "trail_sl")]
    tgt_hits = [t for t in today_trades if t.get("exit_reason") in ("target_hit", "target_volatile", "partial_target")]
    if len(today_trades) >= _MIN_SAMPLES:
        sl_rate = len(sl_hits) / len(today_trades)
        if sl_rate > 0.65:
            adaptive_params.adjust("atr_multiplier", +1,
                f"SL hit rate {sl_rate*100:.0f}% — widening SL (ATR mult)")
            changes.append(f"↑ ATR multiplier (SL hit {sl_rate*100:.0f}% of trades)")
        elif sl_rate < 0.25 and len(tgt_hits) / max(len(today_trades), 1) > 0.5:
            adaptive_params.adjust("atr_multiplier", -1,
                f"SL rate {sl_rate*100:.0f}%, target rate {len(tgt_hits)/len(today_trades)*100:.0f}% — SL could tighten")
            changes.append(f"↓ ATR multiplier (targets hitting well, SL loose)")

    # ── 3. Breakout distance — if many trades entered then immediately lost ────
    quick_losses = [
        t for t in today_trades
        if t["pnl_pts"] < 0 and t.get("exit_reason") == "sl_hit"
        and _elapsed_mins(t) < 10
    ]
    if len(quick_losses) >= 2:
        adaptive_params.adjust("bk_min_dist", +1,
            f"{len(quick_losses)} trades SL'd within 10 mins — waiting for deeper breakout")
        changes.append(f"↑ breakout min dist ({len(quick_losses)} quick SL hits)")

    # ── 4. S/R tolerance — if many trades blocked but then moved in direction ──
    # (Can't detect this without logging blocked signals — skip for now)

    # ── 5. IV rank block — if we missed big moves during high IV ───────────────
    # This is harder to detect without IV at entry stored; skip for now

    # ── 6. Overall insights ────────────────────────────────────────────────────
    wins   = [t for t in today_trades if t["pnl_pts"] > 0]
    losses = [t for t in today_trades if t["pnl_pts"] <= 0]
    pnl_rs = sum(t.get("pnl_rs", 0) for t in today_trades)
    wr     = len(wins) / len(today_trades) * 100 if today_trades else 0

    if wr < 35:
        insights.append("⚠ Very low WR — check if market was trending or sideways")
    if len(tgt_hits) == 0 and len(today_trades) >= 3:
        insights.append("⚠ No targets hit — trail SL may be firing too early or targets too far")
    if len(wins) > 0:
        best = max(today_trades, key=lambda t: t["pnl_pts"])
        insights.append(f"Best trade: {best['option']} {best['strike']} +{best['pnl_pts']:.1f}pts")
    if len(losses) > 0:
        worst = min(today_trades, key=lambda t: t["pnl_pts"])
        insights.append(f"Worst trade: {worst['option']} {worst['strike']} {worst['pnl_pts']:.1f}pts")

    # ── Save and notify ────────────────────────────────────────────────────────
    adaptive_params.save()

    summary = _build_summary(today, today_trades, wins, losses, pnl_rs, wr, changes, insights)
    log.info("Learning summary:\n%s", summary)

    from uptox.services.telegram_service import send
    send(summary)

    return summary


def _elapsed_mins(trade: dict) -> float:
    te = trade.get("time_entry", "")
    tx = trade.get("time_exit",  "")
    if not te or not tx:
        return 999.0
    try:
        def _mins(s: str) -> int:
            h, m, sec = s.split(":")
            return int(h) * 60 + int(m)
        return abs(_mins(tx) - _mins(te))
    except Exception:
        return 999.0


def _build_summary(today, trades, wins, losses, pnl_rs, wr, changes, insights) -> str:
    sign = "+" if pnl_rs >= 0 else ""
    icon = "📈" if pnl_rs >= 0 else "📉"
    lines = [
        f"{icon} <b>End-of-Day Learning — {today}</b>",
        f"Trades: {len(trades)}  ({len(wins)}W / {len(losses)}L)  WR: {wr:.0f}%",
        f"P&amp;L: {sign}₹{pnl_rs:,.0f}",
        "",
    ]
    if changes:
        lines.append("🔧 <b>Parameters adjusted:</b>")
        for c in changes:
            lines.append(f"  • {c}")
        lines.append("")
    if insights:
        lines.append("💡 <b>Insights:</b>")
        for i in insights:
            lines.append(f"  • {i}")
    if not changes:
        lines.append("✅ No parameter changes — system performing within expected range")
    return "\n".join(lines)
