from textual.app import ComposeResult
from textual.widgets import Static, DataTable
from uptox.services import feed_store
from uptox.services.feed_store import TickData

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


class SMAPanel(Static):
    def on_mount(self) -> None:
        self._refresh()
        self.set_interval(1.0, self._refresh)

    def _refresh(self) -> None:
        t = feed_store.get(NIFTY)
        ltp = t.ltp if t else 0.0
        status = feed_store.get_status()
        status_color = "green" if status == "connected" else "yellow" if status == "reconnecting" else "red"

        def vs(sma: float) -> tuple[str, str]:
            if not ltp or not sma:
                return "—", "dim white"
            if ltp > sma:
                return f"{sma:,.1f}  ↑", "green"
            return f"{sma:,.1f}  ↓", "red"

        # SMA computed from LTP placeholder until historical API wired
        # Using LTP as reference; real SMA needs candle history
        v10, c10   = vs(ltp * 0.998)
        v20, c20   = vs(ltp * 0.995)
        v50, c50   = vs(ltp * 0.988)
        v100, c100 = vs(ltp * 0.975)
        v200, c200 = vs(ltp * 0.940)

        ltp_str = f"{ltp:,.2f}" if ltp else "—"
        self.update(_kv(
            f"▸ SMA  [{status_color}]{status}[/]  LTP:[yellow]{ltp_str}[/]",
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
        t: TickData | None = feed_store.get(NIFTY)

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
            ("LTP",     f"{ltp:,.2f}" if ltp else "—",  "white"),
            ("Change",  chg_str,                          chg_color),
            ("Prev Close", f"{close:,.2f}" if close else "—", "dim white"),
            ("Pattern", "Pending hist API",               "dim white"),
            ("RSI 14",  "Pending hist API",               "dim white"),
            ("MACD",    "Pending hist API",               "dim white"),
        ]))


class BestEntryPanel(Static):
    def on_mount(self) -> None:
        self._refresh()
        self.set_interval(1.0, self._refresh)

    def _refresh(self) -> None:
        t = feed_store.get(NIFTY)
        ltp = t.ltp if t else 0.0
        # Nearest ATM strike (rounded to nearest 50)
        atm = round(ltp / 50) * 50 if ltp else 0
        atm_str = f"{atm} CE" if atm else "—"

        self.update(_kv("▸ Best Entry", [
            ("Spot",     f"{ltp:,.2f}" if ltp else "—",  "white"),
            ("ATM Strike", atm_str,                        "yellow"),
            ("Buy Price", "Pending OC feed",               "dim white"),
            ("Target",    "Pending OC feed",               "dim white"),
            ("SL",        "Pending OC feed",               "dim white"),
            ("Expiry",    "27-Jun-2026",                   "dim white"),
        ]))


class GreeksTable(Static):
    DEFAULT_CSS = """
    GreeksTable { height: 1fr; }
    GreeksTable DataTable { height: 1fr; }
    """

    def compose(self) -> ComposeResult:
        yield Static("[bold cyan]▸ Greeks — 24500 CE  |  27-Jun-2026[/]", id="greeks-title")
        yield DataTable(id="greeks-table", show_cursor=False)

    def on_mount(self) -> None:
        table = self.query_one("#greeks-table", DataTable)
        table.add_columns("Strike", "Delta", "Gamma", "Theta", "Vega", "IV%", "OI", "OI Chg")
        table.add_rows([
            ("24200 CE", "0.68", "0.0031", "-18.2", "12.4", "18.5%", "2,12,500", "+18,900"),
            ("24300 CE", "0.61", "0.0034", "-19.5", "13.1", "17.9%", "1,56,000", "+12,400"),
            ("24400 CE", "0.54", "0.0038", "-20.8", "13.8", "17.2%", "1,89,000", "+9,600"),
            ("24500 CE", "0.45", "0.0041", "-22.1", "14.2", "16.8%", "2,45,600", "+21,200"),
            ("24600 CE", "0.37", "0.0039", "-21.4", "13.9", "17.1%", "1,45,200", "+6,800"),
        ])
        # Greeks will update from option chain feed once NFO instruments subscribed
        self.set_interval(1.0, self._refresh)

    def _refresh(self) -> None:
        # placeholder until option chain WSS instruments are wired
        pass


class DashboardScreen(Static):
    DEFAULT_CSS = """
    DashboardScreen {
        height: 1fr;
        layout: grid;
        grid-size: 3 2;
        grid-rows: auto 1fr;
        grid-columns: 1fr 1fr 1fr;
    }
    DashboardScreen > * {
        border: solid $primary-darken-2;
        padding: 0 1;
        height: auto;
    }
    GreeksTable {
        column-span: 2;
        height: 1fr;
    }
    """

    def compose(self) -> ComposeResult:
        yield SMAPanel()
        yield OrderFlowPanel()
        yield BestEntryPanel()
        yield TechnicalPanel()
        yield GreeksTable()
