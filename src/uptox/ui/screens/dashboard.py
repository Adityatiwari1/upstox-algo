from textual.app import ComposeResult
from textual.containers import ScrollableContainer
from textual.reactive import reactive
from textual.widgets import Static
from uptox.services import feed_store, sma_service, option_chain_service
from uptox.services.sma_service import TimeFrame, ALL_TFS

NIFTY = "NSE_INDEX|Nifty 50"
BANK  = "NSE_INDEX|Nifty Bank"


def _fmt(v: float, dec: int = 2) -> str:
    return f"{v:,.{dec}f}" if v else "—"


def _signal(delta: float, cum: float, oi_chg: float) -> tuple[str, str]:
    score = 0
    if delta > 0: score += 1
    if cum > 0:   score += 1
    if oi_chg > 0: score += 1
    if score >= 2:
        return "● BUY", "green"
    elif score == 0:
        return "● SELL", "red"
    return "◐ NEUTRAL", "yellow"


def _kv(title: str, rows: list[tuple[str, str, str]]) -> str:
    lines = [f"[bold cyan]{title}[/]"]
    for label, value, color in rows:
        lines.append(f"[dim]{label:<12}[/] [{color}]{value}[/]")
    return "\n".join(lines)


class OverallAnalysisPanel(Static):
    def on_mount(self) -> None:
        self._refresh()
        self.set_interval(1.0, self._refresh)

    def _refresh(self) -> None:
        t = feed_store.get(NIFTY)
        ltp = t.ltp if t else 0.0

        # ATM strike (nearest 50)
        atm = round(ltp / 50) * 50 if ltp else 0

        # SMA trend score from 5min (most responsive)
        smas  = sma_service.get_smas(NIFTY, TimeFrame.M5)
        sma20 = smas.get(20)
        sma50 = smas.get(50)
        sma_ready = sma20 is not None and sma50 is not None
        sma_score = 0
        if ltp and sma20: sma_score += (1 if ltp > sma20 else -1)
        if ltp and sma50: sma_score += (1 if ltp > sma50 else -1)

        # Order flow score
        cum       = feed_store.cum_delta(NIFTY)
        raw_delta = (t.tbq - t.tsq) if t else 0.0
        oi_chg    = t.change_oi if t else 0.0
        flow_score = (
            (1 if raw_delta > 0 else -1 if raw_delta < 0 else 0)
            + (1 if cum > 0 else -1 if cum < 0 else 0)
            + (1 if oi_chg > 0 else -1 if oi_chg < 0 else 0)
        )

        total = sma_score + flow_score  # range: -5 … +5

        # ── trend label ───────────────────────────────────────────────────
        if total >= 4:
            trend, trend_color, option = "STRONG BULL", "green",  "CE"
        elif total >= 2:
            trend, trend_color, option = "BULLISH",     "green",  "CE"
        elif total <= -4:
            trend, trend_color, option = "STRONG BEAR", "red",    "PE"
        elif total <= -2:
            trend, trend_color, option = "BEARISH",     "red",    "PE"
        else:
            trend, trend_color, option = "NEUTRAL",     "yellow", "—"

        # ── trade validity ────────────────────────────────────────────────
        no_trade_reason = None

        if not ltp:
            no_trade_reason = "No live data"
        elif not sma_ready:
            no_trade_reason = "SMA loading..."
        elif option == "—":
            no_trade_reason = "Trend too weak"
        elif sma_score != 0 and flow_score != 0 and (sma_score > 0) != (flow_score > 0):
            # SMA and order flow point opposite directions
            no_trade_reason = f"SMA vs Flow divergence"
        elif abs(flow_score) == 1 and abs(sma_score) <= 1:
            # Both barely pointing one way — not enough conviction
            no_trade_reason = "Weak conviction"

        tradeable = no_trade_reason is None

        # ── render ────────────────────────────────────────────────────────
        opt_color  = "green" if option == "CE" else "red" if option == "PE" else "dim white"
        strike_str = f"{atm} {option}" if atm and tradeable else (f"{atm}" if atm else "—")
        ltp_str    = f"{ltp:,.2f}" if ltp else "—"

        # Live buy price — WSS only, no REST fallback
        buy_price = 0.0
        if tradeable and atm:
            key = option_chain_service.get_key(atm, option)
            live = feed_store.get(key) if key else None
            buy_price = live.ltp if live else 0.0

        buy_str = f"{buy_price:.1f}" if buy_price else "—"

        if tradeable:
            action_line = f"[dim]{'Action':<12}[/] [bold {opt_color}]BUY {option} @ {atm}[/]"
        else:
            action_line = f"[dim]{'Action':<12}[/] [bold red]NO TRADE — {no_trade_reason}[/]"

        lines = [
            f"[bold cyan]▸ Overall Analysis[/]  [dim]LTP:[/] [yellow]{ltp_str}[/]",
            action_line,
            f"[dim]{'Strike':<12}[/] [yellow]{strike_str if tradeable else '—'}[/]",
            f"[dim]{'Option':<12}[/] [bold {opt_color}]{option if tradeable else '—'}[/]",
            f"[dim]{'Buy Price':<12}[/] [{opt_color}]{buy_str}[/]",
            f"[dim]{'Trend':<12}[/] [bold {trend_color}]{trend}[/]",
            f"[dim]{'Score':<12}[/] [dim white]{total:+d}/5  SMA {sma_score:+d}  Flow {flow_score:+d}[/]",
        ]
        self.update("\n".join(lines))


class SMAPanel(Static):
    current_tf: reactive[TimeFrame] = reactive(TimeFrame.M5)

    def on_mount(self) -> None:
        self._refresh()
        self.set_interval(1.0, self._refresh)

    def cycle_tf(self) -> None:
        idx = ALL_TFS.index(self.current_tf)
        self.current_tf = ALL_TFS[(idx + 1) % len(ALL_TFS)]

    def watch_current_tf(self, _: TimeFrame) -> None:
        self._refresh()

    def _refresh(self) -> None:
        t = feed_store.get(NIFTY)
        ltp = t.ltp if t else 0.0
        status = feed_store.get_status()
        status_color = "green" if status == "connected" else "yellow" if status == "reconnecting" else "red"
        tf = self.current_tf

        def vs(sma: float | None) -> tuple[str, str]:
            if not ltp or sma is None:
                return "—", "dim white"
            if ltp > sma:
                return f"{sma:,.1f}  ↑", "green"
            return f"{sma:,.1f}  ↓", "red"

        smas = sma_service.get_smas(NIFTY, tf)
        v10,  c10  = vs(smas[10])
        v20,  c20  = vs(smas[20])
        v50,  c50  = vs(smas[50])
        v100, c100 = vs(smas[100])
        v200, c200 = vs(smas[200])

        ltp_str = f"{ltp:,.2f}" if ltp else "—"
        self.update(_kv(
            f"▸ SMA [{tf.value}]  [{status_color}]{status}[/]  LTP:[yellow]{ltp_str}[/]",
            [
                ("SMA 10",  v10,  c10),
                ("SMA 20",  v20,  c20),
                ("SMA 50",  v50,  c50),
                ("SMA 100", v100, c100),
                ("SMA 200", v200, c200),
            ]
        ))


class OrderFlowPanel(Static):
    def on_mount(self) -> None:
        self._refresh()
        self.set_interval(1.0, self._refresh)

    def _refresh(self) -> None:
        t = feed_store.get(NIFTY)

        if t:
            raw_delta = t.tbq - t.tsq
            cum       = feed_store.cum_delta(NIFTY)
            ratio     = (t.tbq / t.tsq) if t.tsq else 0.0
            sig, sig_color = _signal(raw_delta, cum, t.change_oi)
            delta_color = "green" if raw_delta >= 0 else "red"
            cum_color   = "green" if cum >= 0 else "red"
            oi_color    = "green" if t.change_oi >= 0 else "red"
            mbp_bias    = "Buyers" if t.mbp_buy > t.mbp_sell else "Sellers"
            mbp_color   = "green" if t.mbp_buy > t.mbp_sell else "red"
        else:
            raw_delta = cum = ratio = 0.0
            sig, sig_color = "◐ NEUTRAL", "yellow"
            delta_color = cum_color = oi_color = mbp_color = "dim white"
            mbp_bias = "—"

        def row(label: str, value: str, color: str) -> tuple[str, str, str]:
            return (label, value, color)

        lines = [
            "[bold cyan]▸ Order Flow Analysis[/]",
            "[dim]── Delta ──────────────────────[/]",
            f"[dim]{'Delta':<12}[/] [{delta_color}]{_fmt(raw_delta, 0)}[/]",
            f"[dim]{'Cum. Delta':<12}[/] [{cum_color}]{_fmt(cum, 0)}[/]",
            f"[dim]{'Buy/Sell':<12}[/] [white]{_fmt(ratio)}  ({'↑ Buyers' if ratio >= 1 else '↓ Sellers'})[/]",
            "[dim]── Imbalance ───────────────────[/]",
            f"[dim]{'MBP Bias':<12}[/] [{mbp_color}]{mbp_bias}[/]",
            f"[dim]{'Buy Qty':<12}[/] [green]{_fmt(t.tbq, 0) if t else '—'}[/]",
            f"[dim]{'Sell Qty':<12}[/] [red]{_fmt(t.tsq, 0) if t else '—'}[/]",
            "[dim]── OI / Liquidity ─────────────[/]",
            f"[dim]{'OI':<12}[/] [white]{_fmt(t.oi, 0) if t else '—'}[/]",
            f"[dim]{'OI Change':<12}[/] [{oi_color}]{_fmt(t.change_oi, 0) if t else '—'}[/]",
            f"[dim]{'Volume':<12}[/] [white]{_fmt(t.vtt, 0) if t else '—'}[/]",
            "[dim]── Signal ──────────────────────[/]",
            f"[bold]{'Action':<12}[/] [bold {sig_color}]{sig}[/]",
        ]
        self.update("\n".join(lines))


class TechnicalPanel(Static):
    def on_mount(self) -> None:
        self._refresh()
        self.set_interval(1.0, self._refresh)

    def _refresh(self) -> None:
        t = feed_store.get(NIFTY)
        ltp   = t.ltp if t else 0.0
        close = t.close if t else 0.0
        chg   = ltp - close if (ltp and close) else 0.0
        chg_p = (chg / close * 100) if close else 0.0
        chg_color = "green" if chg >= 0 else "red"
        chg_str = f"{chg:+.2f} ({chg_p:+.2f}%)" if ltp else "—"

        self.update(_kv("▸ Technical", [
            ("LTP",        f"{ltp:,.2f}" if ltp else "—",      "white"),
            ("Change",     chg_str,                              chg_color),
            ("Prev Close", f"{close:,.2f}" if close else "—",   "dim white"),
            ("Pattern",    "—",                                  "dim white"),
        ]))


class BestEntryPanel(Static):
    def on_mount(self) -> None:
        self._refresh()
        self.set_interval(1.0, self._refresh)

    def _refresh(self) -> None:
        t = feed_store.get(NIFTY)
        ltp = t.ltp if t else 0.0
        atm = round(ltp / 50) * 50 if ltp else 0

        # Determine CE or PE from order flow + SMA (same logic as OverallAnalysisPanel)
        smas = sma_service.get_smas(NIFTY, TimeFrame.M5)
        sma20, sma50 = smas.get(20), smas.get(50)
        sma_score = 0
        if ltp and sma20: sma_score += 1 if ltp > sma20 else -1
        if ltp and sma50: sma_score += 1 if ltp > sma50 else -1
        cum = feed_store.cum_delta(NIFTY)
        raw_delta = (t.tbq - t.tsq) if t else 0.0
        flow_score = (1 if raw_delta > 0 else -1 if raw_delta < 0 else 0) + \
                     (1 if cum > 0 else -1 if cum < 0 else 0)
        opt_type = "CE" if (sma_score + flow_score) > 0 else "PE" if (sma_score + flow_score) < 0 else None

        # Live option LTP — WSS only
        buy_price = 0.0
        if atm and opt_type:
            key = option_chain_service.get_key(atm, opt_type)
            live = feed_store.get(key) if key else None
            buy_price = live.ltp if live else 0.0

        target = round(buy_price * 1.5, 1) if buy_price else 0.0
        sl     = round(buy_price * 0.6, 1) if buy_price else 0.0
        expiry = option_chain_service.get_expiry() or "—"

        opt_color = "green" if opt_type == "CE" else "red" if opt_type == "PE" else "dim white"

        self.update(_kv("▸ Best Entry", [
            ("Spot",       f"{ltp:,.2f}" if ltp else "—",                    "white"),
            ("ATM Strike", f"{atm} {opt_type}" if atm and opt_type else "—",  "yellow"),
            ("Option",     opt_type or "—",                                   opt_color),
            ("Buy Price",  f"{buy_price:.1f}" if buy_price else "—",          opt_color),
            ("Target",     f"{target:.1f}" if target else "—",                "green"),
            ("SL",         f"{sl:.1f}" if sl else "—",                        "red"),
            ("Expiry",     expiry if expiry else "—",                          "dim white"),
        ]))


class GreeksTable(Static):
    def on_mount(self) -> None:
        self._refresh()
        self.set_interval(1.0, self._refresh)

    def _refresh(self) -> None:
        t = feed_store.get(NIFTY)
        ltp = t.ltp if t else 0.0
        expiry = option_chain_service.get_expiry()

        if not ltp or not expiry:
            self.update("[bold cyan]▸ Option Chain[/]\n[dim white]Waiting for data...[/]")
            return

        atm = round(ltp / 50) * 50
        strikes = option_chain_service.get_atm_strikes(ltp, n=5)

        hdr = (
            f"[bold cyan]▸ Option Chain[/]  "
            f"[dim]ATM:[/][yellow]{atm}[/]  "
            f"[dim]Expiry:[/][white]{expiry}[/]\n"
            f"[dim]{'CE LTP':>8} {'CΔ':>6} {'C OI':>7}  {'Strike':^6}  {'PE LTP':>8} {'PΔ':>6} {'P OI':>7}[/]"
        )
        rows = [hdr]

        for strike in sorted(strikes):
            ce_key = option_chain_service.get_key(strike, "CE")
            pe_key = option_chain_service.get_key(strike, "PE")

            # WSS live ticks only — show "—" until first tick received
            ce_t = feed_store.get(ce_key) if ce_key else None
            pe_t = feed_store.get(pe_key) if pe_key else None

            ce_ltp   = ce_t.ltp   if ce_t else None
            ce_delta = ce_t.delta if ce_t else None
            ce_oi    = ce_t.oi    if ce_t else None
            pe_ltp   = pe_t.ltp   if pe_t else None
            pe_delta = pe_t.delta if pe_t else None
            pe_oi    = pe_t.oi    if pe_t else None

            atm_mark = "bold yellow" if strike == atm else "white"

            def _ltp(v):  return f"{v:8.1f}"   if v is not None else f"{'—':>8}"
            def _dlt(v):  return f"{v:+6.3f}"  if v is not None else f"{'—':>6}"
            def _oi(v):   return f"{v/1e5:6.1f}L" if v is not None else f"{'—':>7}"

            row = (
                f"[green]{_ltp(ce_ltp)} {_dlt(ce_delta)} {_oi(ce_oi)}[/]"
                f"  [{atm_mark}]{strike:^6}[/]  "
                f"[red]{_ltp(pe_ltp)} {_dlt(pe_delta)} {_oi(pe_oi)}[/]"
            )
            rows.append(row)

        self.update("\n".join(rows))


class DashboardScreen(ScrollableContainer):
    DEFAULT_CSS = """
    DashboardScreen {
        height: 1fr;
        layout: vertical;
    }
    DashboardScreen > * {
        border: solid $primary-darken-2;
        padding: 0 1;
        width: 100%;
        height: auto;
    }
    GreeksTable {
        height: auto;
    }
    GreeksTable DataTable {
        height: auto;
    }
    """

    def compose(self) -> ComposeResult:
        yield OverallAnalysisPanel()
        yield SMAPanel()
        yield OrderFlowPanel()
        yield BestEntryPanel()
        yield TechnicalPanel()
        yield GreeksTable()
