"""
Monthly PDF performance report. Sent via Telegram on the last trading day
of each month. Charts via matplotlib, layout via reportlab.

Rupee symbol fix: reportlab's default Helvetica font has no glyph for ₹
(U+20B9), so it rendered blank/broken. Registers Windows Arial.ttf instead,
which has full ₹ support, for every text element.
"""
from __future__ import annotations

import logging
from pathlib import Path
from datetime import date

logging.getLogger("matplotlib").setLevel(logging.WARNING)
log = logging.getLogger(__name__)

_REPORT_DIR = Path("logs/reports")
_FONT_REGULAR = "C:/Windows/Fonts/arial.ttf"
_FONT_BOLD    = "C:/Windows/Fonts/arialbd.ttf"

_GREEN = "#1a7a3c"
_RED   = "#b32020"
_BLUE  = "#1a5fb4"
_GREY  = "#666666"


def _register_fonts() -> None:
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    if "Arial" not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont("Arial", _FONT_REGULAR))
        pdfmetrics.registerFont(TTFont("Arial-Bold", _FONT_BOLD))


def _setup_matplotlib() -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.font_manager as fm
    fm.fontManager.addfont(_FONT_REGULAR)
    import matplotlib.pyplot as plt
    plt.rcParams["font.family"] = "Arial"
    plt.rcParams["axes.edgecolor"] = "#cccccc"
    plt.rcParams["axes.grid"] = True
    plt.rcParams["grid.color"] = "#eeeeee"
    plt.rcParams["grid.linewidth"] = 0.5


# ── Aggregation helpers ──────────────────────────────────────────────────────

_ACRONYMS = {"sl", "atr", "oi", "pcr", "iv", "bk", "rr", "sr", "ce", "pe"}


def _humanize(s: str) -> str:
    """snake_case or ALLCAPS raw values into readable labels: trail_sl -> Trail SL."""
    if not s:
        return "Unknown"
    words = str(s).replace("-", "_").split("_")
    out = []
    for w in words:
        out.append(w.upper() if w.lower() in _ACRONYMS else w.capitalize())
    return " ".join(out)


_KNOWN_ENTRY_TYPES = {"retest", "momentum", "sustained", "reentry"}


def _entry_type(trade: dict) -> str:
    reason = trade.get("entry_reason", "") or ""
    first = reason.split("|")[0].strip().lower()
    return first if first in _KNOWN_ENTRY_TYPES else "other"


def _hour_bucket(trade: dict) -> str:
    t = trade.get("time_entry", "")
    if not t:
        return "unknown"
    h = int(t.split(":")[0])
    if h < 10:
        return "09:30-10:00"
    if h < 12:
        return "10:00-12:00"
    if h < 14:
        return "12:00-14:00"
    return "14:00-15:25"


def _stats(trades: list[dict]) -> dict:
    wins   = [t for t in trades if t.get("pnl_pts", 0) > 0]
    losses = [t for t in trades if t.get("pnl_pts", 0) <= 0]
    pnl_rs = sum(t.get("pnl_rs", 0) for t in trades)
    avg_w  = sum(t.get("pnl_rs", 0) for t in wins)   / len(wins)   if wins   else 0.0
    avg_l  = sum(t.get("pnl_rs", 0) for t in losses) / len(losses) if losses else 0.0
    return {
        "total": len(trades), "wins": len(wins), "losses": len(losses),
        "win_rate": (len(wins) / len(trades) * 100) if trades else 0.0,
        "pnl_rs": pnl_rs, "avg_win": avg_w, "avg_loss": avg_l,
    }


def _max_drawdown(trades: list[dict], start_capital: float) -> tuple[float, float]:
    cap = start_capital
    peak = start_capital
    dd = 0.0
    for t in sorted(trades, key=lambda x: (x.get("date", ""), x.get("time_entry", ""))):
        cap += t.get("pnl_rs", 0)
        peak = max(peak, cap)
        dd = max(dd, peak - cap)
    return dd, (dd / peak * 100 if peak else 0.0)


# ── Chart builders (return PNG file paths) ──────────────────────────────────

def _chart_equity_curve(trades: list[dict], start_capital: float, out: Path) -> Path:
    import matplotlib.pyplot as plt
    ordered = sorted(trades, key=lambda x: (x.get("date", ""), x.get("time_entry", "")))
    cap = start_capital
    xs, ys = [0], [start_capital]
    for i, t in enumerate(ordered, 1):
        cap += t.get("pnl_rs", 0)
        xs.append(i)
        ys.append(cap)
    fig, ax = plt.subplots(figsize=(7.2, 3.2), dpi=150)
    ax.plot(xs, ys, color=_BLUE, linewidth=2)
    ax.fill_between(xs, ys, start_capital, alpha=0.08, color=_BLUE)
    ax.axhline(start_capital, color=_GREY, linewidth=0.8, linestyle="--")
    ax.set_xlabel("Trade number")
    ax.set_ylabel("Capital (Rs)")
    ax.set_title("Equity Curve")
    fig.tight_layout()
    path = out / "equity_curve.png"
    fig.savefig(path)
    plt.close(fig)
    return path


def _chart_daily_pnl(trades: list[dict], out: Path) -> Path:
    import matplotlib.pyplot as plt
    by_day: dict[str, float] = {}
    for t in trades:
        d = t.get("date", "")
        by_day[d] = by_day.get(d, 0.0) + t.get("pnl_rs", 0)
    days = sorted(by_day)
    vals = [by_day[d] for d in days]
    colors = [_GREEN if v >= 0 else _RED for v in vals]
    labels = [d[5:] for d in days]   # MM-DD
    fig, ax = plt.subplots(figsize=(7.2, 3.0), dpi=150)
    ax.bar(labels, vals, color=colors)
    ax.axhline(0, color=_GREY, linewidth=0.8)
    ax.set_ylabel("Day P&L (Rs)")
    ax.set_title("Daily P&L")
    ax.tick_params(axis="x", rotation=60, labelsize=7)
    fig.tight_layout()
    path = out / "daily_pnl.png"
    fig.savefig(path)
    plt.close(fig)
    return path


def _chart_entry_type_pie(trades: list[dict], out: Path) -> Path:
    import matplotlib.pyplot as plt
    counts: dict[str, int] = {}
    for t in trades:
        et = _humanize(_entry_type(t))
        counts[et] = counts.get(et, 0) + 1
    fig, ax = plt.subplots(figsize=(4.2, 3.4), dpi=150)
    palette = ["#1a5fb4", "#2ecc71", "#f39c12", "#9b59b6", "#e74c3c"]
    ax.pie(
        counts.values(), labels=counts.keys(), autopct="%1.0f%%",
        colors=palette[:len(counts)], textprops={"fontsize": 8},
    )
    ax.set_title("Entry Type Mix")
    fig.tight_layout()
    path = out / "entry_type_pie.png"
    fig.savefig(path)
    plt.close(fig)
    return path


def _chart_time_of_day(trades: list[dict], out: Path) -> Path:
    import matplotlib.pyplot as plt
    order = ["09:30-10:00", "10:00-12:00", "12:00-14:00", "14:00-15:25"]
    by_bucket: dict[str, float] = {b: 0.0 for b in order}
    for t in trades:
        b = _hour_bucket(t)
        if b in by_bucket:
            by_bucket[b] += t.get("pnl_rs", 0)
    vals = [by_bucket[b] for b in order]
    colors = [_GREEN if v >= 0 else _RED for v in vals]
    fig, ax = plt.subplots(figsize=(7.2, 2.8), dpi=150)
    ax.bar(order, vals, color=colors)
    ax.axhline(0, color=_GREY, linewidth=0.8)
    ax.set_ylabel("P&L (Rs)")
    ax.set_title("P&L by Time of Day")
    ax.tick_params(axis="x", labelsize=8)
    fig.tight_layout()
    path = out / "time_of_day.png"
    fig.savefig(path)
    plt.close(fig)
    return path


# ── PDF builder ───────────────────────────────────────────────────────────────

def generate(month: date | None = None) -> Path:
    """Build the monthly PDF report and return its file path."""
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import cm
    from reportlab.lib import colors as rl_colors
    from reportlab.platypus import (
        SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
        Image, HRFlowable,
    )
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.enums import TA_CENTER

    from uptox.services import trade_book, capital_service, adaptive_params

    _register_fonts()
    _setup_matplotlib()

    target_month = month or date.today()
    month_str = target_month.strftime("%Y-%m")
    month_label = target_month.strftime("%B %Y")

    all_trades = trade_book.get_all_trades()
    trades = [t for t in all_trades if t.get("date", "").startswith(month_str)]

    _REPORT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = _REPORT_DIR / f"monthly_report_{month_str}.pdf"

    cap_now  = capital_service.get_capital()
    cap_base = capital_service.get_base_capital()
    s = _stats(trades)
    cap_month_start = cap_now - s["pnl_rs"]
    dd_rs, dd_pct = _max_drawdown(trades, cap_month_start)

    doc = SimpleDocTemplate(
        str(out_path), pagesize=A4,
        leftMargin=1.8*cm, rightMargin=1.8*cm, topMargin=1.8*cm, bottomMargin=1.8*cm,
        title=f"Addy's Algo Trading, Monthly Report {month_label}",
    )

    H1 = ParagraphStyle("H1", fontName="Arial-Bold", fontSize=16, leading=20,
                         textColor=rl_colors.HexColor("#1a5fb4"), spaceAfter=6, spaceBefore=14)
    TITLE = ParagraphStyle("TITLE", fontName="Arial-Bold", fontSize=24, leading=30,
                            textColor=rl_colors.HexColor("#1a5fb4"), alignment=TA_CENTER, spaceAfter=4)
    SUB = ParagraphStyle("SUB", fontName="Arial", fontSize=12, leading=16,
                          textColor=rl_colors.HexColor("#555555"), alignment=TA_CENTER, spaceAfter=2)
    BODY = ParagraphStyle("BODY", fontName="Arial", fontSize=9.5, leading=14, spaceAfter=4)
    BULLET = ParagraphStyle("BULLET", parent=BODY, leftIndent=14, bulletIndent=2)
    NOTE = ParagraphStyle("NOTE", fontName="Arial", fontSize=8.5, leading=11,
                           textColor=rl_colors.HexColor("#777777"))
    BIG = ParagraphStyle("BIG", fontName="Arial-Bold", fontSize=28, leading=34,
                          alignment=TA_CENTER, spaceAfter=6)

    def table_style(header_bg=_BLUE):
        return TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), rl_colors.HexColor(header_bg)),
            ("TEXTCOLOR",  (0, 0), (-1, 0), rl_colors.white),
            ("FONTNAME",   (0, 0), (-1, 0), "Arial-Bold"),
            ("FONTNAME",   (0, 1), (-1, -1), "Arial"),
            ("FONTSIZE",   (0, 0), (-1, -1), 9),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [rl_colors.HexColor("#f7f8fa"), rl_colors.white]),
            ("GRID",       (0, 0), (-1, -1), 0.3, rl_colors.HexColor("#dddddd")),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ("VALIGN",     (0, 0), (-1, -1), "MIDDLE"),
        ])

    story = []

    # ── Section 1: Cover ──────────────────────────────────────────────────
    story.append(Spacer(1, 40))
    story.append(Paragraph("Addy's Algo Trading", TITLE))
    story.append(Paragraph("Monthly Performance Report", SUB))
    story.append(Paragraph(month_label, SUB))
    story.append(Spacer(1, 30))
    sign = "+" if s["pnl_rs"] >= 0 else ""
    color = _GREEN if s["pnl_rs"] >= 0 else _RED
    story.append(Paragraph(
        f"<font color='{color}'>{sign}₹{s['pnl_rs']:,.0f}</font>",
        BIG,
    ))
    story.append(Paragraph(
        f"Capital: ₹{cap_base:,.0f} to ₹{cap_now:,.0f}  "
        f"({(cap_now-cap_base)/cap_base*100:+.1f}% lifetime)",
        ParagraphStyle("SUB3", parent=SUB, fontSize=10),
    ))
    story.append(Spacer(1, 60))
    story.append(HRFlowable(width="100%", thickness=0.5, color=rl_colors.HexColor("#cccccc")))

    # ── Section 2: Executive Summary ─────────────────────────────────────
    story.append(Paragraph("Executive Summary", H1))
    story.append(Table([
        ["Metric", "Value"],
        ["Total trades", str(s["total"])],
        ["Win rate", f"{s['win_rate']:.1f}%  ({s['wins']}W / {s['losses']}L)"],
        ["Total P&L", f"{'+' if s['pnl_rs']>=0 else ''}₹{s['pnl_rs']:,.0f}"],
        ["Avg win", f"₹{s['avg_win']:,.0f}"],
        ["Avg loss", f"₹{s['avg_loss']:,.0f}"],
        ["Max drawdown", f"₹{dd_rs:,.0f}  ({dd_pct:.1f}% of peak)"],
    ], colWidths=[7*cm, 9*cm]))
    story[-1].setStyle(table_style())
    story.append(Spacer(1, 10))

    if not trades:
        story.append(Paragraph("No trades recorded this month.", BODY))
        doc.build(story)
        return out_path

    # ── Section 3: Equity Curve ───────────────────────────────────────────
    story.append(Paragraph("Equity Curve", H1))
    img_path = _chart_equity_curve(trades, cap_month_start, _REPORT_DIR)
    story.append(Image(str(img_path), width=17*cm, height=7.5*cm))

    # ── Section 4: Daily P&L ──────────────────────────────────────────────
    story.append(Paragraph("Daily P&amp;L", H1))
    img_path = _chart_daily_pnl(trades, _REPORT_DIR)
    story.append(Image(str(img_path), width=17*cm, height=7*cm))

    # ── Section 5: Entry Type Breakdown ──────────────────────────────────
    story.append(Paragraph("Entry Type Breakdown", H1))
    by_entry: dict[str, list] = {}
    for t in trades:
        by_entry.setdefault(_entry_type(t), []).append(t)
    rows = [["Entry Type", "Trades", "Win Rate", "P&L"]]
    for et, lst in sorted(by_entry.items(), key=lambda x: -len(x[1])):
        st = _stats(lst)
        rows.append([_humanize(et), str(st["total"]), f"{st['win_rate']:.0f}%",
                     f"{'+' if st['pnl_rs']>=0 else ''}₹{st['pnl_rs']:,.0f}"])
    story.append(Table(rows, colWidths=[5*cm, 3*cm, 3.5*cm, 4.5*cm]))
    story[-1].setStyle(table_style())
    story.append(Spacer(1, 8))
    img_path = _chart_entry_type_pie(trades, _REPORT_DIR)
    story.append(Image(str(img_path), width=9*cm, height=7.3*cm))

    # ── Section 6: Exit Reason Breakdown ─────────────────────────────────
    story.append(Paragraph("Exit Reason Breakdown", H1))
    by_exit: dict[str, list] = {}
    for t in trades:
        by_exit.setdefault(t.get("exit_reason", "unknown"), []).append(t)
    rows = [["Exit Reason", "Trades", "Win Rate", "P&L"]]
    for er, lst in sorted(by_exit.items(), key=lambda x: -len(x[1])):
        st = _stats(lst)
        rows.append([_humanize(er), str(st["total"]), f"{st['win_rate']:.0f}%",
                     f"{'+' if st['pnl_rs']>=0 else ''}₹{st['pnl_rs']:,.0f}"])
    story.append(Table(rows, colWidths=[5*cm, 3*cm, 3.5*cm, 4.5*cm]))
    story[-1].setStyle(table_style())

    # ── Section 7: Regime Performance ────────────────────────────────────
    story.append(Paragraph("Performance by Market Regime", H1))
    def regime_of(t):
        atr = t.get("atr_at_entry", 0) or 0
        if atr >= 35: return "TRENDING"
        if atr <= 20: return "CONSOLIDATING"
        return "NORMAL"
    by_regime: dict[str, list] = {}
    for t in trades:
        by_regime.setdefault(regime_of(t), []).append(t)
    rows = [["Regime", "Trades", "Win Rate", "P&L"]]
    for rg in ["TRENDING", "NORMAL", "CONSOLIDATING"]:
        lst = by_regime.get(rg, [])
        if not lst:
            continue
        st = _stats(lst)
        rows.append([_humanize(rg), str(st["total"]), f"{st['win_rate']:.0f}%",
                     f"{'+' if st['pnl_rs']>=0 else ''}₹{st['pnl_rs']:,.0f}"])
    story.append(Table(rows, colWidths=[5*cm, 3*cm, 3.5*cm, 4.5*cm]))
    story[-1].setStyle(table_style())

    # ── Section 8: Expiry vs Normal Day ──────────────────────────────────
    story.append(Paragraph("Expiry Day vs Normal Day", H1))
    def _was_expiry_day(t: dict) -> bool:
        # expiry_day flag may be absent on older records; date==expiry is always reliable
        if t.get("expiry_day"):
            return True
        return bool(t.get("date")) and t.get("date") == t.get("expiry")

    exp_trades = [t for t in trades if _was_expiry_day(t)]
    norm_trades = [t for t in trades if not _was_expiry_day(t)]
    rows = [["Day Type", "Trades", "Win Rate", "P&L"]]
    for label, lst in [("Expiry Day", exp_trades), ("Normal Day", norm_trades)]:
        if not lst:
            continue
        st = _stats(lst)
        rows.append([label, str(st["total"]), f"{st['win_rate']:.0f}%",
                     f"{'+' if st['pnl_rs']>=0 else ''}₹{st['pnl_rs']:,.0f}"])
    story.append(Table(rows, colWidths=[5*cm, 3*cm, 3.5*cm, 4.5*cm]))
    story[-1].setStyle(table_style())

    # ── Section 9: CE vs PE ────────────────────────────────────────────────
    story.append(Paragraph("CE vs PE Breakdown", H1))
    ce_trades = [t for t in trades if t.get("option") == "CE"]
    pe_trades = [t for t in trades if t.get("option") == "PE"]
    rows = [["Option", "Trades", "Win Rate", "P&L"]]
    for label, lst in [("CE", ce_trades), ("PE", pe_trades)]:
        if not lst:
            continue
        st = _stats(lst)
        rows.append([label, str(st["total"]), f"{st['win_rate']:.0f}%",
                     f"{'+' if st['pnl_rs']>=0 else ''}₹{st['pnl_rs']:,.0f}"])
    story.append(Table(rows, colWidths=[5*cm, 3*cm, 3.5*cm, 4.5*cm]))
    story[-1].setStyle(table_style())

    # ── Section 9b: Nifty Points Captured ────────────────────────────────
    story.append(Paragraph("Nifty 50 Points Captured", H1))
    story.append(Paragraph(
        "How many points the underlying Nifty 50 index itself moved in the trade's "
        "favor, separate from the option premium P&amp;L above. Helps judge whether "
        "small option gains came from a real index move or just IV/theta noise.",
        BODY,
    ))
    def _nifty_pts_row(label: str, lst: list[dict]) -> list:
        if not lst:
            return [label, "0", "-", "-"]
        total = sum(t.get("spot_directional_pts", 0) for t in lst)
        avg   = total / len(lst)
        avg_peak = sum(t.get("spot_peak_move_pts", 0) for t in lst) / len(lst)
        return [label, str(len(lst)), f"{total:+.1f}", f"{avg:+.1f}  (peak avg {avg_peak:+.1f})"]
    rows = [["Group", "Trades", "Total Pts", "Avg Pts/Trade"]]
    rows.append(_nifty_pts_row("All Trades", trades))
    rows.append(_nifty_pts_row("CE", ce_trades))
    rows.append(_nifty_pts_row("PE", pe_trades))
    story.append(Table(rows, colWidths=[4*cm, 3*cm, 4*cm, 6*cm]))
    story[-1].setStyle(table_style())

    # ── Section 10: Time of Day ──────────────────────────────────────────
    story.append(Paragraph("Performance by Time of Day", H1))
    img_path = _chart_time_of_day(trades, _REPORT_DIR)
    story.append(Image(str(img_path), width=17*cm, height=6.5*cm))

    # ── Section 11: Left on Table ─────────────────────────────────────────
    story.append(Paragraph("Trail SL: Left on Table Analysis", H1))
    win_trades = [t for t in trades if t.get("pnl_pts", 0) > 0 and t.get("peak_price")]
    if win_trades:
        avg_left = sum(t["peak_price"] - t.get("exit_price", t["peak_price"]) for t in win_trades) / len(win_trades)
        avg_entry = sum(t.get("entry_price", 1) for t in win_trades) / len(win_trades)
        left_pct = avg_left / avg_entry * 100 if avg_entry else 0
        story.append(Paragraph(
            f"Average gap between peak price and exit price on winning trades: "
            f"<b>{avg_left:.1f} pts ({left_pct:.1f}% of entry price)</b>. "
            f"Lower is better, it means the trail SL is capturing more of the move before exiting.",
            BODY,
        ))
    else:
        story.append(Paragraph("No winning trades with peak-price data this month.", BODY))

    # ── Section 12: Adaptive Parameter Changes ───────────────────────────
    story.append(Paragraph("Adaptive Parameter Changes", H1))
    changes = [
        c for c in adaptive_params.get_change_log(200)
    ]
    if changes:
        rows = [["Parameter", "Old", "New", "Reason"]]
        for c in changes[-15:]:
            rows.append([_humanize(c.get("param", "")), str(c.get("old", "")), str(c.get("new", "")),
                         Paragraph(c.get("reason", ""), NOTE)])
        story.append(Table(rows, colWidths=[3.5*cm, 2*cm, 2*cm, 9.5*cm]))
        story[-1].setStyle(table_style())
    else:
        story.append(Paragraph("No parameter adjustments recorded this month.", BODY))

    # ── Section 13: Auto-Generated Insights ──────────────────────────────
    story.append(Paragraph("Key Insights", H1))
    insights = []
    if by_entry:
        best_et = max(by_entry.items(), key=lambda x: _stats(x[1])["win_rate"])
        worst_et = min(by_entry.items(), key=lambda x: _stats(x[1])["win_rate"])
        if len(by_entry) > 1:
            insights.append(
                f"{_humanize(best_et[0])} entries had the highest win rate "
                f"({_stats(best_et[1])['win_rate']:.0f}%), {_humanize(worst_et[0])} the lowest "
                f"({_stats(worst_et[1])['win_rate']:.0f}%)."
            )
    if by_regime:
        best_rg = max(by_regime.items(), key=lambda x: _stats(x[1])["pnl_rs"])
        insights.append(f"{_humanize(best_rg[0])} market conditions produced the most P&L this month.")
    if exp_trades and norm_trades:
        exp_wr = _stats(exp_trades)["win_rate"]
        norm_wr = _stats(norm_trades)["win_rate"]
        insights.append(f"Expiry day win rate: {exp_wr:.0f}% vs normal day: {norm_wr:.0f}%.")
    if not insights:
        insights.append("Not enough variety in trades this month for deeper pattern insights.")
    for i in insights:
        story.append(Paragraph(i, BULLET))

    doc.build(story)

    # Chart PNGs are embedded in the PDF now; the standalone files aren't needed
    for chart_name in ("equity_curve.png", "daily_pnl.png", "entry_type_pie.png", "time_of_day.png"):
        try:
            (_REPORT_DIR / chart_name).unlink(missing_ok=True)
        except Exception as e:
            log.warning("Could not remove temp chart %s: %s", chart_name, e)

    log.info("Monthly report generated: %s", out_path)
    return out_path
