from textual.app import ComposeResult
from textual.widgets import Static, DataTable, Label, Select
from textual.containers import Horizontal

from uptox.services import feed_store, option_chain_service
from uptox.config.settings import settings

NIFTY = "NSE_INDEX|Nifty 50"
_SELECT_BLANK = [("Loading...", "loading")]


class OptionChainScreen(Static):
    DEFAULT_CSS = """
    OptionChainScreen {
        height: 1fr;
        overflow-y: auto;
        padding: 0 1;
    }
    OptionChainScreen .chain-header {
        layout: horizontal;
        height: 3;
        align: left middle;
        margin-bottom: 1;
    }
    OptionChainScreen .header-item  { margin-right: 3; color: $text-muted; }
    OptionChainScreen .header-value { color: $accent;   text-style: bold; margin-right: 3; }
    OptionChainScreen .spot-value   { color: $success;  text-style: bold; margin-right: 3; }
    """

    def compose(self) -> ComposeResult:
        with Horizontal(classes="chain-header"):
            yield Label("Symbol:",  classes="header-item")
            yield Label("NIFTY 50", classes="header-value")
            yield Label("Spot:",    classes="header-item")
            yield Label("—",        classes="spot-value",   id="spot-label")
            yield Label("Expiry:",  classes="header-item")
            yield Select(_SELECT_BLANK, id="expiry-select", value="loading")
            yield Label("", id="expiry-status", classes="header-item")
        yield DataTable(id="chain-table", show_cursor=False)

    def on_mount(self) -> None:
        table = self.query_one("#chain-table", DataTable)
        table.add_columns(
            "OI (C)", "OI Chg", "LTP (C)", "IV (C)",
            "Strike",
            "IV (P)", "LTP (P)", "OI Chg", "OI (P)",
        )
        self._expiries_loaded = False
        self._tick = 0
        self._fetch_pending = False
        self._refresh()
        self.set_interval(1.0, self._refresh)

    def on_select_changed(self, event: Select.Changed) -> None:
        # Select.NULL fires when options are replaced programmatically — skip it
        if event.value is Select.BLANK or event.value is None:
            return
        expiry = str(event.value)
        _skip = {"loading", "Select.NULL", "Select.BLANK", "None", ""}
        if expiry in _skip or expiry == option_chain_service.get_expiry():
            return
        self.app.run_worker(
            option_chain_service.switch_expiry(expiry, settings.access_token),
            name=f"oc-switch-{expiry}",
        )

    def _refresh(self) -> None:
        self._tick += 1
        t = feed_store.get(NIFTY)
        ltp = (t.ltp if t else 0.0) or option_chain_service.get_spot()
        expiry = option_chain_service.get_expiry()
        expiries = option_chain_service.get_all_expiries()
        status_lbl = self.query_one("#expiry-status", Label)

        # ── Populate Select once expiries arrive ──────────────────────────────
        if expiries and not self._expiries_loaded:
            sel = self.query_one("#expiry-select", Select)
            sel.set_options([(e, e) for e in expiries])
            if expiry:
                sel.value = expiry
            self._expiries_loaded = True
            status_lbl.update("")

        # ── No expiries yet — retry fetch, show progress ───────────────────────
        if not self._expiries_loaded:
            if not settings.access_token:
                status_lbl.update("[red]No access token — login first[/]")
            elif self._tick == 5 and not self._fetch_pending:
                # First auto-retry after 5s
                self._fetch_pending = True
                status_lbl.update("[yellow]Retrying...[/]")
                self.app.run_worker(
                    option_chain_service.fetch_and_prime(settings.access_token),
                    name="oc-prime-retry",
                )
            elif self._tick > 5 and self._tick % 30 == 0 and not self._fetch_pending:
                # Retry every 30s after that
                self._fetch_pending = True
                status_lbl.update("[yellow]Retrying...[/]")
                self.app.run_worker(
                    option_chain_service.fetch_and_prime(settings.access_token),
                    name=f"oc-prime-retry-{self._tick}",
                )
            elif self._tick <= 5:
                waited = self._tick
                status_lbl.update(f"[dim]Loading... {waited}s[/]")
            else:
                status_lbl.update("[dim]Waiting for data...[/]")
                self._fetch_pending = False  # allow next retry window

        # Update spot label (mark snapshot when no live feed)
        spot_str = f"{ltp:,.2f}" if ltp else "—"
        if ltp and not (t and t.ltp):
            spot_str += " [snap]"
        self.query_one("#spot-label", Label).update(spot_str)

        if not ltp or not expiry:
            return

        atm = round(ltp / 50) * 50
        strikes = option_chain_service.get_atm_strikes(ltp, n=7)

        table = self.query_one("#chain-table", DataTable)
        table.clear()

        for strike in sorted(strikes):
            ce_key = option_chain_service.get_key(strike, "CE")
            pe_key = option_chain_service.get_key(strike, "PE")

            # Live feed preferred; fall back to REST chain snapshot
            ce_t  = feed_store.get(ce_key) if ce_key else None
            pe_t  = feed_store.get(pe_key) if pe_key else None
            ce_sn = option_chain_service.get_option(strike, "CE") or {}
            pe_sn = option_chain_service.get_option(strike, "PE") or {}

            def _get(tick, sn, field):
                live = getattr(tick, field, None) if tick else None
                return live if live else sn.get(field)

            def _ltp(v):    return f"{v:.1f}"       if v else "—"
            def _oi(v):     return f"{v/1000:.1f}K" if v else "—"
            def _oichg(v):  return f"{v:+,.0f}"     if v else "—"
            def _iv(v):     return f"{v:.1f}%"       if v else "—"

            strike_label = f"★ {strike} ATM" if strike == atm else str(strike)

            table.add_row(
                _oi(_get(ce_t, ce_sn, "oi")),
                _oichg(_get(ce_t, ce_sn, "change_oi")),
                _ltp(_get(ce_t, ce_sn, "ltp")),
                _iv(_get(ce_t, ce_sn, "iv")),
                strike_label,
                _iv(_get(pe_t, pe_sn, "iv")),
                _ltp(_get(pe_t, pe_sn, "ltp")),
                _oichg(_get(pe_t, pe_sn, "change_oi")),
                _oi(_get(pe_t, pe_sn, "oi")),
            )
