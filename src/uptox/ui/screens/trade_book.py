"""Trade Book screen — nested tabs: Open Trade | Performance | Analysis | History."""
from textual.app import ComposeResult
from textual.containers import ScrollableContainer
from textual.widget import Widget
from textual.widgets import Static, Input, TabbedContent, TabPane
from uptox.services import trade_book, trailing_sl_service

_OUTCOME_COLOR = {
    "WIN":        "green",
    "TARGET_HIT": "bold green",
    "LOSS":       "red",
    "SL_HIT":     "bold red",
    "BREAKEVEN":  "yellow",
    "OPEN":       "cyan",
}



# ── Open Trade ────────────────────────────────────────────────────────────────

class OpenTradePanel(Static):

    def on_mount(self) -> None:
        self._refresh()
        self.set_interval(1.0, self._refresh)

    def _refresh(self) -> None:
        ot = trade_book.get_open_trade()
        ts = trailing_sl_service.get_status()

        if not ot:
            self.update(
                "[bold cyan]▸ Open Trade[/]\n\n"
                "[dim white]No open trade.  System auto-enters on next valid signal.[/]"
            )
            return

        pnl_pts  = ot.get("pnl_pts", 0.0)
        pnl_rs   = ot.get("pnl_rs",  0.0)
        cur      = ot.get("current_price", ot["entry_price"])
        pnl_col  = "green" if pnl_pts >= 0 else "red"
        opt_col  = "green" if ot["option"] == "CE" else "red"
        hz       = ot.get("hero_zero", False)
        multiple = round(cur / ot["entry_price"], 2) if ot["entry_price"] else 1.0

        trail_lines = []
        if ts["active"]:
            tsl_col  = "bold red" if ts["stopped_out"] else "red"
            sl_label = "Step SL" if hz else "Trail SL"
            sl_detail = (
                f"  [dim]{multiple:.2f}x entry[/]"
                if hz else
                f"  [dim]({int((ts['trail_pct'] or 0)*100)}% below peak)[/]"
            )
            trail_lines = [
                "",
                f"[dim]── {'Hero/Zero Step SL' if hz else 'Trailing SL'} {'⚡' if hz else ''}─────────────────[/]",
                f"[dim]{'Entry':<16}[/] [white]{ts['entry']:.1f}[/]",
                f"[dim]{'Peak':<16}[/] [yellow]{ts['peak']:.1f}[/]"
                + (f"  [dim]({multiple:.1f}x)[/]" if hz else ""),
                f"[dim]{sl_label:<16}[/] [{tsl_col}]{ts['trail_sl']:.1f}[/]{sl_detail}",
                f"[dim]{'P&L':<16}[/] [{pnl_col}]{ts['pnl_pts']:+.1f}pts[/]",
                f"[dim]{'Status':<16}[/] " + (
                    "[bold red]STOPPED OUT — press x[/]" if ts["stopped_out"]
                    else "[bold green]In Profit ✓[/]" if ts["in_profit"]
                    else "[yellow]Tracking...[/]"
                ),
            ]

        hz_badge = "  [bold yellow]⚡ HERO/ZERO[/]" if hz else ""
        self.update("\n".join([
            f"[bold cyan]▸ Open Trade[/]{hz_badge}", "",
            f"[dim]{'Option':<16}[/] [{opt_col}]{ot['option']} {ot['strike']}[/]"
            f"   [dim]Expiry:[/] [white]{ot['expiry']}[/]",
            f"[dim]{'Entry Time':<16}[/] [white]{ot['time_entry']}[/]"
            f"   [dim]Date:[/] [white]{ot['date']}[/]",
            "",
            "[dim]── Prices ───────────────────────────────────[/]",
            f"[dim]{'Entry Price':<16}[/] [white]{ot['entry_price']:.1f}[/]"
            f"   [dim]Current:[/] [white]{cur:.1f}[/]",
            f"[dim]{'SL (initial)':<16}[/] [red]{ot['sl']:.1f}[/]"
            f"   [dim]Target:[/] "
            + ("[bold green]" if ot.get("target_crossed") else "[green]")
            + f"{ot['target']:.1f}[/]"
            + (
                "  [bold green]✓ TRAILING[/]"
                f"  [dim]vel={ot.get('velocity_ppm','?')}pts/min  IV={ot.get('iv_at_target','?')}[/]"
                if ot.get("target_crossed") and ot.get("market_mode") == "TRENDING"
                else "  [bold yellow]✓ VOLATILE → closed[/]"
                if ot.get("target_crossed") and ot.get("market_mode") == "VOLATILE"
                else ""
            ),
            f"[dim]{'Spot (entry)':<16}[/] [white]{ot.get('spot_entry', 0):.1f}[/]",
            "",
            "[dim]── Position ──────────────────────────────────[/]",
            f"[dim]{'Lots':<16}[/] [white]{ot['lots']}[/]"
            f"   [dim]Invested:[/] [white]₹{ot['invested']:,.0f}[/]",
            f"[dim]{'Delta':<16}[/] [white]{ot['delta'] if ot['delta'] else '—'}[/]",
            "",
            "[dim]── P&L ─────────────────────────────────────────[/]",
            f"[dim]{'Unrealized pts':<16}[/] [{pnl_col}]{pnl_pts:+.1f}[/]",
            f"[dim]{'Unrealized ₹':<16}[/] [{pnl_col}]{pnl_rs:+,.0f}[/]",
        ] + trail_lines))


# ── Performance ───────────────────────────────────────────────────────────────

class PerformancePanel(Static):

    def on_mount(self) -> None:
        self._refresh()
        self.set_interval(2.0, self._refresh)

    def _refresh(self) -> None:
        trades = trade_book.get_analysis_trades()
        s      = trade_book.get_stats(trades)
        w      = trade_book.get_analysis_window()
        total_all = len(trade_book.get_all_trades())

        if s["total"] == 0:
            self.update(
                f"[bold cyan]▸ Performance[/]  [dim](last {w} of {total_all} trades)[/]\n\n"
                "[dim white]No closed trades yet.[/]"
            )
            return

        # ₹ equivalents computed from per-trade pnl_rs
        wins_rs   = [t["pnl_rs"] for t in trades if t["pnl_pts"] > 0]
        losses_rs = [t["pnl_rs"] for t in trades if t["pnl_pts"] <= 0]
        best_rs   = max((t["pnl_rs"] for t in trades), default=0.0)
        worst_rs  = min((t["pnl_rs"] for t in trades), default=0.0)
        avg_win_rs  = sum(wins_rs)   / len(wins_rs)   if wins_rs   else 0.0
        avg_loss_rs = sum(losses_rs) / len(losses_rs) if losses_rs else 0.0
        wr = s["win_rate"] / 100
        exp_rs = round(wr * avg_win_rs - (1 - wr) * abs(avg_loss_rs), 0)

        # max drawdown in both pts and ₹
        run_pts = run_rs = peak_pts = peak_rs = dd_pts = dd_rs = 0.0
        for t in trades:
            run_pts += t["pnl_pts"]; run_rs += t["pnl_rs"]
            peak_pts = max(peak_pts, run_pts); peak_rs = max(peak_rs, run_rs)
            dd_pts   = min(dd_pts, run_pts - peak_pts)
            dd_rs    = min(dd_rs,  run_rs  - peak_rs)

        pnl_col = "green" if s["total_pnl_pts"] >= 0 else "red"
        exp_col = "green" if s["expectancy"]    >= 0 else "red"
        wr_col  = "green" if s["win_rate"] >= 60 else "yellow" if s["win_rate"] >= 45 else "red"

        if s["win_rate"] >= 55 and s["expectancy"] > 5:
            rating, r_col = "GOOD EDGE",      "bold green"
        elif s["win_rate"] >= 45 and s["expectancy"] >= 0:
            rating, r_col = "NEUTRAL",         "yellow"
        else:
            rating, r_col = "REFINE STRATEGY", "bold red"

        def _rs(v: float) -> str:
            return f"₹{v:+,.0f}"

        self.update(
            f"[bold cyan]▸ Performance[/]  [{r_col}]{rating}[/]"
            f"  [dim](last {w} of {total_all} trades)[/]\n\n"
            "[dim]── Trade Count ──────────────────────────────[/]\n"
            f"[dim]{'Total (window)':<16}[/] [white]{s['total']}[/]\n"
            f"[dim]{'Wins':<16}[/] [green]{s['wins']}[/]\n"
            f"[dim]{'Losses':<16}[/] [red]{s['losses']}[/]\n"
            f"[dim]{'Win Rate':<16}[/] [{wr_col}]{s['win_rate']}%[/]\n\n"
            "[dim]── P&L ────────────────────────────────────────[/]\n"
            f"[dim]{'Total':<16}[/] [{pnl_col}]{s['total_pnl_pts']:+.1f} pts   {_rs(s['total_pnl_rs'])}[/]\n"
            f"[dim]{'Avg Win':<16}[/] [green]{s['avg_win']:+.1f} pts   {_rs(avg_win_rs)}[/]\n"
            f"[dim]{'Avg Loss':<16}[/] [red]{s['avg_loss']:.1f} pts   {_rs(avg_loss_rs)}[/]\n"
            f"[dim]{'Best Trade':<16}[/] [green]{s['best']:+.1f} pts   {_rs(best_rs)}[/]\n"
            f"[dim]{'Worst Trade':<16}[/] [red]{s['worst']:.1f} pts   {_rs(worst_rs)}[/]\n\n"
            "[dim]── Risk ───────────────────────────────────────[/]\n"
            f"[dim]{'Expectancy':<16}[/] [{exp_col}]{s['expectancy']:+.1f} pts   {_rs(exp_rs)} /trade[/]\n"
            f"[dim]{'Achieved RR':<16}[/] [white]{s['avg_rr']:.2f}[/]\n"
            f"[dim]{'Max Drawdown':<16}[/] [red]{dd_pts:.1f} pts   {_rs(dd_rs)}[/]"
        )


# ── Analysis ──────────────────────────────────────────────────────────────────

class AnalysisPanel(Widget):
    DEFAULT_CSS = """
    AnalysisPanel { layout: vertical; height: auto; width: 100%; padding: 0 1; }
    AnalysisPanel #analysis-display { height: auto; }
    AnalysisPanel #analysis-input {
        display: none;
        height: 3;
        background: $surface;
        color: $text;
        border: tall $primary;
    }
    AnalysisPanel #analysis-input.active { display: block; }
    """

    def compose(self) -> ComposeResult:
        yield Static(id="analysis-display")
        yield Input(
            placeholder="Enter number of trades to analyse (e.g. 50) → Enter confirms, Esc cancels",
            id="analysis-input",
        )

    def on_mount(self) -> None:
        self._refresh()
        self.set_interval(2.0, self._refresh)

    def open_input(self) -> None:
        inp = self.query_one("#analysis-input", Input)
        inp.add_class("active")
        inp.focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "analysis-input":
            try:
                n = int(event.value.strip())
                trade_book.set_analysis_window(n)
            except ValueError:
                pass
            self._close_input()

    def on_key(self, event) -> None:
        if event.key == "escape":
            inp = self.query_one("#analysis-input", Input)
            if "active" in inp.classes:
                self._close_input()
                event.stop()

    def _close_input(self) -> None:
        inp = self.query_one("#analysis-input", Input)
        inp.remove_class("active")
        inp.clear()

    def _refresh(self) -> None:
        trades    = trade_book.get_analysis_trades()
        s         = trade_book.get_stats(trades)
        w         = trade_book.get_analysis_window()
        total_all = len(trade_book.get_all_trades())
        lines     = [
            f"[bold cyan]▸ Analysis[/]"
            f"  [dim](last {w} of {total_all} trades — [/][dim white]n[/][dim] to change)[/]",
            "",
        ]

        if s["total"] < 2:
            lines.append("[dim white]Need ≥2 closed trades.[/]")
            self.query_one("#analysis-display", Static).update("\n".join(lines))
            return

        # ── CE vs PE ──────────────────────────────────────────────────────────
        lines.append("[dim]── CE vs PE ────────────────────────────────[/]")
        for label, col in (("CE", "green"), ("PE", "red")):
            grp = [t for t in trades if t["option"] == label]
            if not grp:
                continue
            wins   = sum(1 for t in grp if t["pnl_pts"] > 0)
            pnl_p  = sum(t["pnl_pts"] for t in grp)
            pnl_rs = sum(t["pnl_rs"]  for t in grp)
            wr     = wins / len(grp) * 100
            pc     = "green" if pnl_p >= 0 else "red"
            lines.append(
                f"  [{col}]{label}[/]  "
                f"[white]{len(grp)} trades  {wins}W/{len(grp)-wins}L  {wr:.0f}% WR[/]  "
                f"[{pc}]{pnl_p:+.1f}pts  ₹{pnl_rs:+,.0f}[/]"
            )

        # ── Time of Day ───────────────────────────────────────────────────────
        lines += ["", "[dim]── Best Time to Trade ─────────────────────[/]"]
        buckets: dict[str, list] = {"09–11": [], "11–13": [], "13–15": []}
        for t in trades:
            h = int((t.get("time_entry") or "00:00:00")[:2])
            bucket = "09–11" if h < 11 else "11–13" if h < 13 else "13–15"
            buckets[bucket].append(t)
        for slot, grp in buckets.items():
            if not grp:
                continue
            wins  = sum(1 for t in grp if t["pnl_pts"] > 0)
            pnl_p = sum(t["pnl_pts"] for t in grp)
            pnl_r = sum(t["pnl_rs"]  for t in grp)
            col   = "green" if pnl_p >= 0 else "red"
            lines.append(
                f"  [dim]{slot}[/]  [white]{len(grp)} trades  {wins}W/{len(grp)-wins}L[/]"
                f"  [{col}]{pnl_p:+.1f}pts  ₹{pnl_r:+,.0f}[/]"
            )

        # ── Exit Reasons ──────────────────────────────────────────────────────
        lines += ["", "[dim]── How Trades Exit ────────────────────────[/]"]
        reasons: dict[str, list] = {}
        for t in trades:
            r = t.get("exit_reason") or "unknown"
            reasons.setdefault(r, []).append(t)
        for reason, grp in sorted(reasons.items(), key=lambda x: -len(x[1])):
            pnl_p = sum(t["pnl_pts"] for t in grp)
            pnl_r = sum(t["pnl_rs"]  for t in grp)
            pct   = len(grp) / len(trades) * 100
            col   = "green" if pnl_p >= 0 else "red"
            lines.append(
                f"  [dim]{reason:<20}[/] [white]{len(grp)}x ({pct:.0f}%)[/]"
                f"  [{col}]{pnl_p:+.1f}pts  ₹{pnl_r:+,.0f}[/]"
            )

        # ── Verdict ───────────────────────────────────────────────────────────
        lines += ["", "[dim]── Verdict ──────────────────────────────────[/]"]
        if s["expectancy"] > 0 and s["win_rate"] >= 50:
            lines.append("  [green]✓ Positive expectancy — system has edge.[/]")
        if s["avg_rr"] < 1.0 and s["total"] >= 5:
            lines.append("  [red]↓ RR < 1 — losses bigger than wins on average.[/]")
        if s["win_rate"] < 40:
            lines.append("  [red]↓ Win rate < 40% — entry conditions too loose.[/]")
        sl_pct = len(reasons.get("sl_hit", [])) / max(s["total"], 1)
        if sl_pct > 0.5:
            lines.append("  [yellow]⚠ >50% SL hits — consider tightening entry.[/]")
        trail_pct = len(reasons.get("trail_sl", [])) / max(s["total"], 1)
        if trail_pct > 0.4 and s["expectancy"] > 0:
            lines.append("  [green]✓ Trail SL catching profits well.[/]")
        if not any("✓" in l or "↓" in l or "⚠" in l for l in lines[-5:]):
            lines.append("  [dim white]No issues detected yet.[/]")

        self.query_one("#analysis-display", Static).update("\n".join(lines))


# ── Trade History ─────────────────────────────────────────────────────────────

class TradeHistoryPanel(Static):

    def on_mount(self) -> None:
        self._refresh()
        self.set_interval(2.0, self._refresh)

    def _refresh(self) -> None:
        trades  = trade_book.get_all_trades()
        last20  = list(reversed(trades))[:20]
        total   = len(trades)

        hdr = (
            f"[bold cyan]▸ Trade History[/]  [dim](last 20 of {total})[/]\n\n"
            "[dim]"
            f"{'#':>3}  {'Date':<11}{'In':>6} {'Out':>6}"
            f"  {'Strike':>6} {'T':<3}"
            f"  {'Entry':>6} {'Exit':>6}"
            f"  {'P&L pts':>8}  {'P&L ₹':>9}  {'Outcome':<12}  Reason"
            "[/]"
        )

        if not last20:
            self.update(hdr + "\n[dim white]No closed trades yet.[/]")
            return

        rows = [hdr]
        for i, t in enumerate(last20, 1):
            col   = _OUTCOME_COLOR.get(t["outcome"], "white")
            pnl_c = "green" if t["pnl_pts"] >= 0 else "red"
            opt_c = "green" if t["option"] == "CE" else "red"
            exit_p = f"{t['exit_price']:.1f}" if t["exit_price"] is not None else "—"
            rows.append(
                f"[dim]{i:>3}[/]  "
                f"[dim]{t['date']:<11}[/]"
                f"[white]{t.get('time_entry','—'):>6}[/] "
                f"[dim]{t.get('time_exit','—'):>6}[/]"
                f"  [{opt_c}]{t['strike']:>6} {t['option']:<3}[/]"
                f"  [white]{t['entry_price']:>6.1f}[/] [dim]{exit_p:>6}[/]"
                f"  [{pnl_c}]{t['pnl_pts']:>+8.1f}[/]"
                f"  [{pnl_c}]{t['pnl_rs']:>+9,.0f}[/]"
                f"  [{col}]{t['outcome']:<12}[/]"
                f"  [dim]{t.get('exit_reason','—')}[/]"
            )

        self.update("\n".join(rows))


# ── Screen ────────────────────────────────────────────────────────────────────

class TradeBookScreen(ScrollableContainer):
    DEFAULT_CSS = """
    TradeBookScreen { height: 1fr; }
    TradeBookScreen TabbedContent { height: 1fr; }
    TradeBookScreen TabPane { height: auto; padding: 0; }
    OpenTradePanel, PerformancePanel, AnalysisPanel, TradeHistoryPanel {
        width: 100%;
        height: auto;
        padding: 0 1;
    }
    """

    def compose(self) -> ComposeResult:
        with TabbedContent(initial="tb-open"):
            with TabPane("Open Trade", id="tb-open"):
                yield OpenTradePanel()
            with TabPane("Performance", id="tb-perf"):
                yield PerformancePanel()
            with TabPane("Analysis", id="tb-analysis"):
                yield AnalysisPanel()
            with TabPane("History (last 20)", id="tb-history"):
                yield TradeHistoryPanel()

    def on_key(self, event) -> None:
        """n key opens analysis window input when Analysis tab is active."""
        if event.key == "n":
            try:
                self.query_one(AnalysisPanel).open_input()
                event.stop()
            except Exception:
                pass
