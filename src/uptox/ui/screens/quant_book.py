# src/uptox/ui/screens/quant_book.py
"""Quant Book screen — tab per strategy (Directional | Options Dynamics),
each showing running stats + trade history, so focus mode and both quant
strategies can be compared side by side. Mirrors trade_book.py's
TabbedContent structure."""
from textual.app import ComposeResult
from textual.containers import ScrollableContainer
from textual.widget import Widget
from textual.widgets import Static, TabbedContent, TabPane, DataTable, Label
from uptox.web import quant_data

_OUTCOME_COLOR = {"WIN": "green", "LOSS": "red"}


class _StrategyStatsPanel(Static):
    def __init__(self, strategy_id: str, **kwargs):
        super().__init__(**kwargs)
        self.strategy_id = strategy_id

    def on_mount(self) -> None:
        self._refresh()
        self.set_interval(2.0, self._refresh)

    def _refresh(self) -> None:
        s = quant_data.get_stats(self.strategy_id)
        if s["total"] == 0:
            self.update(f"[bold cyan]▸ {self.strategy_id}[/]\n\n[dim white]No closed trades yet.[/]")
            return
        pnl_col = "green" if s["total_pnl_rs"] >= 0 else "red"
        exp_col = "green" if s["expectancy_rs"] >= 0 else "red"
        wr_col  = "green" if s["win_rate"] >= 55 else "yellow" if s["win_rate"] >= 45 else "red"
        self.update(
            f"[bold cyan]▸ {self.strategy_id}[/]\n\n"
            f"[dim]{'Trades':<16}[/] [white]{s['total']}[/]  "
            f"[dim]Wins:[/] [green]{s['wins']}[/]  [dim]Losses:[/] [red]{s['losses']}[/]\n"
            f"[dim]{'Win Rate':<16}[/] [{wr_col}]{s['win_rate']}%[/]\n"
            f"[dim]{'Total P&L':<16}[/] [{pnl_col}]{s['total_pnl_pts']:+.1f} pts   ₹{s['total_pnl_rs']:+,.0f}[/]\n"
            f"[dim]{'Expectancy':<16}[/] [{exp_col}]{s['expectancy_pts']:+.1f} pts   ₹{s['expectancy_rs']:+,.0f} /trade[/]"
        )


class _StrategyHistoryPanel(Widget):
    DEFAULT_CSS = """
    _StrategyHistoryPanel { height: 1fr; width: 100%; layout: vertical; }
    _StrategyHistoryPanel DataTable { height: 1fr; width: 100%; overflow-x: auto; overflow-y: auto; }
    """

    def __init__(self, strategy_id: str, **kwargs):
        super().__init__(**kwargs)
        self.strategy_id = strategy_id

    def compose(self) -> ComposeResult:
        yield Label("")
        yield DataTable(show_cursor=True)

    def on_mount(self) -> None:
        tbl = self.query_one(DataTable)
        tbl.add_columns("#", "Date", "Opt", "Entry", "Exit", "P&L pts", "P&L ₹", "Reason")
        self._refresh()
        self.set_interval(2.0, self._refresh)

    def _refresh(self) -> None:
        trades = quant_data.get_trades(self.strategy_id)[:20]
        self.query_one(Label).update(f"[dim](last {len(trades)} trades)[/]")
        tbl = self.query_one(DataTable)
        tbl.clear()
        for i, t in enumerate(trades, 1):
            pnl_pts = t.get("pnl_pts", 0.0)
            col = "green" if pnl_pts >= 0 else "red"
            tbl.add_row(
                str(i), t.get("date", "—"), t.get("option", "—"),
                f"{t.get('entry_price', 0):.1f}", f"{t.get('exit_price', 0):.1f}",
                f"[{col}]{pnl_pts:+.1f}[/]", f"[{col}]{t.get('pnl_rs', 0):+,.0f}[/]",
                t.get("exit_reason", "—"),
            )


class QuantBookScreen(ScrollableContainer):
    DEFAULT_CSS = """
    QuantBookScreen { height: 1fr; }
    QuantBookScreen TabbedContent { height: 1fr; }
    QuantBookScreen TabPane { height: 1fr; overflow-y: auto; overflow-x: auto; padding: 0; }
    """

    def compose(self) -> ComposeResult:
        with TabbedContent(initial="qb-directional"):
            with TabPane("Directional", id="qb-directional"):
                yield _StrategyStatsPanel("directional")
                yield _StrategyHistoryPanel("directional")
            with TabPane("Options Dynamics", id="qb-options"):
                yield _StrategyStatsPanel("options_dynamics")
                yield _StrategyHistoryPanel("options_dynamics")
