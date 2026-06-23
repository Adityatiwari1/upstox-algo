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

    # Split expiry-day vs normal-day trades for separate analysis
    expiry_trades = [t for t in today_trades if t.get("expiry_day")]
    normal_trades = [t for t in today_trades if not t.get("expiry_day")]

    changes:  list[str] = []
    insights: list[str] = []

    # ── 1. Score threshold ─────────────────────────────────────────────────────
    low_score  = [t for t in today_trades if abs(t.get("score", 5)) <= 2]
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

    # ── 2. Trail ATR baseline — adjust when trail fires too early or too late ──
    # "Left on table" = peak_price - exit_price. High value = trail fired too early.
    # Analyse only normal-day trades (expiry uses flat 35%, not ATR-scaled)
    trail_exited = [
        t for t in normal_trades
        if t.get("exit_reason") in ("trail_sl", "session_close")
        and t.get("peak_price") and t.get("exit_price")
    ]
    if len(trail_exited) >= _MIN_SAMPLES:
        avg_left = sum(
            t["peak_price"] - t["exit_price"] for t in trail_exited
        ) / len(trail_exited)
        avg_entry = sum(t.get("entry_price", 1) for t in trail_exited) / len(trail_exited)
        left_pct  = avg_left / avg_entry if avg_entry else 0
        avg_atr   = sum(t.get("atr_at_entry", 20) for t in trail_exited) / len(trail_exited)

        if left_pct > 0.25 and avg_atr > 25:
            adaptive_params.adjust("trail_atr_baseline", +1,
                f"Avg left-on-table={left_pct*100:.0f}% at ATR≈{avg_atr:.0f} — trail too tight, raising baseline")
            changes.append(f"↑ trail_atr_baseline (left {left_pct*100:.0f}% on table, ATR≈{avg_atr:.0f})")
        elif left_pct < 0.05:
            sl_rate = sum(1 for t in normal_trades if t.get("exit_reason") == "sl_hit") / max(len(normal_trades), 1)
            if sl_rate <= 0.65:
                adaptive_params.adjust("trail_atr_baseline", -1,
                    f"Left-on-table only {left_pct*100:.0f}% — trail may be too loose, tightening baseline")
                changes.append(f"↓ trail_atr_baseline (trail loose, left only {left_pct*100:.0f}%)")

    # ── 3. Breakout distance — quick SL hits ──────────────────────────────────
    quick_losses = [
        t for t in today_trades
        if t["pnl_pts"] < 0 and t.get("exit_reason") == "sl_hit"
        and _elapsed_mins(t) < 10
    ]
    if len(quick_losses) >= 2:
        adaptive_params.adjust("bk_min_dist", +1,
            f"{len(quick_losses)} trades SL'd within 10 mins — waiting for deeper breakout")
        changes.append(f"↑ breakout min dist ({len(quick_losses)} quick SL hits)")

    # ── 4. Entry type analysis — sustained vs retest ──────────────────────────
    sustained = [t for t in today_trades if "sustained" in (t.get("entry_reason") or "")]
    retest    = [t for t in today_trades if "retest"    in (t.get("entry_reason") or "")]
    if len(sustained) >= _MIN_SAMPLES and len(retest) >= _MIN_SAMPLES:
        sus_wr = sum(1 for t in sustained if t["pnl_pts"] > 0) / len(sustained)
        ret_wr = sum(1 for t in retest    if t["pnl_pts"] > 0) / len(retest)
        if sus_wr < ret_wr - 0.25:
            insights.append(
                f"⚠ Sustained entries WR={sus_wr*100:.0f}% vs retest WR={ret_wr*100:.0f}%"
                f" — sustained signal may be firing in choppy market"
            )
        elif sus_wr > ret_wr + 0.20:
            insights.append(
                f"✓ Sustained entries WR={sus_wr*100:.0f}% outperforming retest {ret_wr*100:.0f}%"
            )

    # ── 5. Expiry day bucket ───────────────────────────────────────────────────
    if expiry_trades:
        exp_wins = [t for t in expiry_trades if t["pnl_pts"] > 0]
        exp_wr   = len(exp_wins) / len(expiry_trades) * 100
        exp_pnl  = sum(t.get("pnl_rs", 0) for t in expiry_trades)
        insights.append(
            f"Expiry day: {len(expiry_trades)} trades  WR={exp_wr:.0f}%  P&L=₹{exp_pnl:+,.0f}"
        )

    # ── 6. Overall insights ────────────────────────────────────────────────────
    wins     = [t for t in today_trades if t["pnl_pts"] > 0]
    losses   = [t for t in today_trades if t["pnl_pts"] <= 0]
    tgt_hits = [t for t in today_trades if t.get("exit_reason") in ("target_hit", "target_volatile", "partial_target")]
    pnl_rs   = sum(t.get("pnl_rs", 0) for t in today_trades)
    wr       = len(wins) / len(today_trades) * 100 if today_trades else 0

    if wr < 35:
        insights.append("⚠ Very low WR — check if market was trending or sideways")
    if len(tgt_hits) == 0 and len(today_trades) >= 3:
        insights.append("⚠ No targets hit — trail SL may be firing too early or targets too far")
    if wins:
        best = max(today_trades, key=lambda t: t["pnl_pts"])
        insights.append(f"Best trade: {best['option']} {best['strike']} +{best['pnl_pts']:.1f}pts")
    if losses:
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
        f"P&amp;L: {sign}₹{pnl_rs:+,.0f}",
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
