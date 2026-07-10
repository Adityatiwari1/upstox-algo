"""
End-of-day learning service.

Runs after market close (3:30 PM). Analyses recent trades AND recent blocked
entries, identifies systematic failures, adjusts adaptive_params in small
steps, saves, and sends a Telegram summary.

Design rules:
- TIGHTEN fast, LOOSEN slow: risk-tightening rules may act on today's
  evidence; loosening rules need a multi-day window.
- Minimum sample sizes before any change; scratch trades (between
  REAL_LOSS_PTS and REAL_WIN_PTS) carry no directional information and are
  excluded from win-rate math.
- Every tightening rule has a reversion path — when the problem stops
  recurring, the parameter steps back toward its default instead of
  ratcheting one way forever.
- At most 1 step per session per parameter; bounded by adaptive_params.
"""
from __future__ import annotations
import json
import logging
from datetime import date, datetime
from pathlib import Path

log = logging.getLogger(__name__)

_MIN_SAMPLES   = 3     # trades needed in a bucket before adjusting from it
_WINDOW_DAYS   = 5     # rolling window (trading days with trades) for loosen/stat rules
_MIN_BLOCKS    = 8     # blocked-entry samples needed before touching sr_tolerance
_RAN_FILE      = Path("logs/learning_ran.txt")   # survives restarts — no double-adjust


def _real_wr(trades: list[dict]) -> tuple[float, int]:
    """Win rate over REAL outcomes only — scratches excluded (a -0.3pt trail
    exit says nothing about whether the rule that fired the trade is good).
    Returns (win_rate, n_meaningful)."""
    from uptox.services.trade_book import REAL_LOSS_PTS, REAL_WIN_PTS
    wins   = [t for t in trades if t.get("pnl_pts", 0) >= REAL_WIN_PTS]
    losses = [t for t in trades if t.get("pnl_pts", 0) <= REAL_LOSS_PTS]
    n = len(wins) + len(losses)
    return (len(wins) / n if n else 0.0), n


def _window_trades(all_trades: list[dict], days: int = _WINDOW_DAYS) -> list[dict]:
    """Trades from the last `days` distinct trading dates (inclusive of today)."""
    dates = sorted({t.get("date", "") for t in all_trades if t.get("date")})[-days:]
    return [t for t in all_trades if t.get("date") in dates]


def _sr_block_evidence(days: int = _WINDOW_DAYS) -> tuple[float, int]:
    """Average 30-min forward Nifty move (signed in the blocked trade's
    direction) across S/R proximity blocks in the window. Positive average
    means the blocks cost money. Returns (avg_move, n)."""
    blocked = Path("logs/blocked_entries.jsonl")
    md_dir  = Path("logs/market_data")
    if not blocked.exists() or not md_dir.exists():
        return 0.0, 0
    md: dict[str, list[tuple[datetime, float]]] = {}
    for f in sorted(md_dir.glob("*.jsonl"))[-days - 2:]:
        rows = []
        for line in f.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            r = json.loads(line)
            if r.get("nifty"):
                rows.append((datetime.fromisoformat(r["ts"]), r["nifty"]))
        if rows:
            md[f.stem] = rows
    if not md:
        return 0.0, 0
    cutoff = sorted(md.keys())[-days:]
    moves: list[float] = []
    for line in blocked.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        day = r["ts"][:10]
        if day not in cutoff or day not in md:
            continue
        reason = r.get("reason", "")
        if not (reason.startswith("Resistance:") or reason.startswith("Support:")):
            continue
        ts   = datetime.fromisoformat(r["ts"])
        sign = 1 if r.get("option") == "CE" else -1
        base = nxt = None
        for t, px in md[day]:
            if base is None and t >= ts:
                base = px
            if base is not None and (t - ts).total_seconds() >= 1800:
                nxt = px
                break
        if base is not None and nxt is not None:
            moves.append(sign * (nxt - base))
    return (sum(moves) / len(moves) if moves else 0.0), len(moves)


def run(force: bool = False) -> str | None:
    """
    Run end-of-day analysis. Idempotent — once per calendar day, persisted to
    disk so an app restart after 15:30 cannot step the same params twice.
    Returns a summary string (also sent to Telegram).
    """
    today = date.today().isoformat()
    if not force:
        try:
            if _RAN_FILE.exists() and _RAN_FILE.read_text(encoding="utf-8").strip() == today:
                return None
        except Exception:
            pass

    from uptox.services import trade_book, adaptive_params
    from uptox.services.trade_book import REAL_LOSS_PTS

    all_trades   = trade_book.get_all_trades()
    today_trades = [t for t in all_trades if t.get("date") == today]
    recent       = _window_trades(all_trades)

    try:
        _RAN_FILE.parent.mkdir(exist_ok=True)
        _RAN_FILE.write_text(today, encoding="utf-8")
    except Exception as e:
        log.error("learning ran-file write: %s", e)

    if not today_trades and not force:
        log.info("Learning: no trades today, skipping adjustment")
        return None

    # Split expiry-day vs normal-day trades for separate analysis
    expiry_trades = [t for t in today_trades if t.get("expiry_day")]
    normal_recent = [t for t in recent if not t.get("expiry_day")]

    changes:  list[str] = []
    insights: list[str] = []

    # ── 1. Score threshold — rolling window, real outcomes only ───────────────
    low_score = [t for t in recent if abs(t.get("score", 5)) <= 2]
    ls_wr, ls_n = _real_wr(low_score)
    if ls_n >= _MIN_SAMPLES:
        if ls_wr < 0.35:
            adaptive_params.adjust("min_score", +1,
                f"score≤2 real-WR={ls_wr*100:.0f}% over {_WINDOW_DAYS}d ({ls_n} real outcomes) — raising threshold")
            changes.append(f"↑ min_score (score≤2 real-WR only {ls_wr*100:.0f}%)")
        elif ls_wr > 0.60 and adaptive_params.get("min_score") > 2:
            adaptive_params.adjust("min_score", -1,
                f"score≤2 real-WR={ls_wr*100:.0f}% over {_WINDOW_DAYS}d — filter was too strict")
            changes.append(f"↓ min_score (score≤2 performing well {ls_wr*100:.0f}%)")

    # ── 2. Trail ATR baseline — rolling window ─────────────────────────────────
    # "Left on table" = peak_price - exit_price. High value = trail fired too early.
    # Normal-day trades only (expiry uses flat 35%, not ATR-scaled).
    trail_exited = [
        t for t in normal_recent
        if t.get("exit_reason") in ("trail_sl", "session_close")
        and t.get("peak_price") and t.get("exit_price")
    ]
    if len(trail_exited) >= _MIN_SAMPLES:
        avg_left  = sum(t["peak_price"] - t["exit_price"] for t in trail_exited) / len(trail_exited)
        avg_entry = sum(t.get("entry_price", 1) for t in trail_exited) / len(trail_exited)
        left_pct  = avg_left / avg_entry if avg_entry else 0
        avg_atr   = sum(t.get("atr_at_entry", 20) for t in trail_exited) / len(trail_exited)

        if left_pct > 0.25 and avg_atr > 25:
            adaptive_params.adjust("trail_atr_baseline", +1,
                f"Avg left-on-table={left_pct*100:.0f}% at ATR≈{avg_atr:.0f} over {_WINDOW_DAYS}d — trail too tight")
            changes.append(f"↑ trail_atr_baseline (left {left_pct*100:.0f}% on table)")
        elif left_pct < 0.05:
            sl_rate = sum(
                1 for t in normal_recent if t.get("exit_reason") == "sl_hit"
            ) / max(len(normal_recent), 1)
            if sl_rate <= 0.65:
                adaptive_params.adjust("trail_atr_baseline", -1,
                    f"Left-on-table only {left_pct*100:.0f}% over {_WINDOW_DAYS}d — trail may be too loose")
                changes.append(f"↓ trail_atr_baseline (left only {left_pct*100:.0f}%)")

    # ── 3. Breakout distance — tighten on TODAY's quick real losses, revert
    #      when the problem stops recurring (no one-way ratchet) ───────────────
    _QUICK_EXITS = ("sl_hit", "mae_exit_10min", "trail_sl")
    def _is_quick_loss(t: dict) -> bool:
        return (t.get("pnl_pts", 0) <= REAL_LOSS_PTS
                and t.get("exit_reason") in _QUICK_EXITS
                and _elapsed_mins(t) < 10)

    quick_today = [t for t in today_trades if _is_quick_loss(t)]
    if len(quick_today) >= 2:
        adaptive_params.adjust("bk_min_dist", +1,
            f"{len(quick_today)} real losses within 10 mins today — waiting for deeper breakout")
        changes.append(f"↑ breakout min dist ({len(quick_today)} quick real losses)")
    else:
        quick_recent = [t for t in recent if _is_quick_loss(t)]
        _bk_default  = 11.0
        if not quick_recent and adaptive_params.get("bk_min_dist") > _bk_default:
            adaptive_params.adjust("bk_min_dist", -1,
                f"No quick losses in {_WINDOW_DAYS}d — stepping back toward default")
            changes.append("↓ breakout min dist (quick-loss problem gone, reverting)")

    # ── 4. S/R tolerance — learn from BLOCKED entries, not just taken trades ──
    # Uses the filter-audit evidence: forward move after each Resistance/Support
    # block. Strongly positive = the blocks cost money → shrink the zone.
    try:
        sr_avg, sr_n = _sr_block_evidence()
        if sr_n >= _MIN_BLOCKS:
            _sr_default = 25.0
            if sr_avg > 8.0:
                adaptive_params.adjust("sr_tolerance", -1,
                    f"S/R blocks cost avg {sr_avg:+.0f}pt/30min over {sr_n} blocks — zone too wide")
                changes.append(f"↓ sr_tolerance (blocks cost {sr_avg:+.0f}pt avg)")
            elif sr_avg < -8.0 and adaptive_params.get("sr_tolerance") < _sr_default:
                adaptive_params.adjust("sr_tolerance", +1,
                    f"S/R blocks saved avg {sr_avg:+.0f}pt/30min over {sr_n} blocks — restore toward default")
                changes.append(f"↑ sr_tolerance (blocks saving {sr_avg:+.0f}pt avg)")
            else:
                insights.append(f"S/R blocks: {sr_n} in {_WINDOW_DAYS}d, avg fwd move {sr_avg:+.1f}pt — no change")
    except Exception as e:
        log.error("learning sr-block analysis: %s", e)

    # ── 5. Entry type analysis — sustained vs retest (window, insight only) ───
    sustained = [t for t in recent if "sustained" in (t.get("entry_reason") or "")]
    retest    = [t for t in recent if "retest"    in (t.get("entry_reason") or "")]
    sus_wr, sus_n = _real_wr(sustained)
    ret_wr, ret_n = _real_wr(retest)
    if sus_n >= _MIN_SAMPLES and ret_n >= _MIN_SAMPLES:
        if sus_wr < ret_wr - 0.25:
            insights.append(
                f"⚠ Sustained real-WR={sus_wr*100:.0f}% vs retest {ret_wr*100:.0f}%"
                f" — sustained may be firing in chop"
            )
        elif sus_wr > ret_wr + 0.20:
            insights.append(
                f"✓ Sustained real-WR={sus_wr*100:.0f}% outperforming retest {ret_wr*100:.0f}%"
            )

    # ── 6. Expiry day bucket ───────────────────────────────────────────────────
    if expiry_trades:
        exp_wins = [t for t in expiry_trades if t["pnl_pts"] > 0]
        exp_wr   = len(exp_wins) / len(expiry_trades) * 100
        exp_pnl  = sum(t.get("pnl_rs", 0) for t in expiry_trades)
        insights.append(
            f"Expiry day: {len(expiry_trades)} trades  WR={exp_wr:.0f}%  P&L=₹{exp_pnl:+,.0f}"
        )

    # ── 7. Overall insights (today) ────────────────────────────────────────────
    wins     = [t for t in today_trades if t["pnl_pts"] > 0]
    losses   = [t for t in today_trades if t["pnl_pts"] <= 0]
    tgt_hits = [t for t in today_trades if t.get("exit_reason") in ("target_hit", "target_volatile", "partial_target")]
    pnl_rs   = sum(t.get("pnl_rs", 0) for t in today_trades)
    wr       = len(wins) / len(today_trades) * 100 if today_trades else 0

    if today_trades and wr < 35:
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
    # Prefer epoch timestamps (robust); fall back to HH:MM:SS strings
    e_ts, x_ts = trade.get("entry_ts"), trade.get("exit_ts")
    if e_ts and x_ts:
        return abs(x_ts - e_ts) / 60.0
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
