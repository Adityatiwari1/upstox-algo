from textual.app import App, ComposeResult
from textual.widgets import Header, Footer, TabbedContent, TabPane
from uptox.ui.screens.dashboard import DashboardScreen
from uptox.ui.screens.orders import OrdersScreen
from uptox.ui.screens.option_chain import OptionChainScreen
from uptox.config.settings import settings


class UptoxApp(App):
    TITLE = "Uptox Trading"
    BINDINGS = [
        ("q", "quit", "Quit"),
        ("1", "switch_tab('dashboard')", "Dashboard"),
        ("2", "switch_tab('orders')", "Orders"),
        ("3", "switch_tab('option-chain')", "Option Chain"),
    ]

    def compose(self) -> ComposeResult:
        yield Header()
        with TabbedContent(initial="dashboard"):
            with TabPane("Dashboard [1]", id="dashboard"):
                yield DashboardScreen()
            with TabPane("Orders [2]", id="orders"):
                yield OrdersScreen()
            with TabPane("Option Chain [3]", id="option-chain"):
                yield OptionChainScreen()
        yield Footer()

    def on_mount(self) -> None:
        if settings.access_token:
            self.run_worker(self._start_feed(), exclusive=True, name="market-feed")

    async def _start_feed(self) -> None:
        from uptox.api.feed_client import run_feed
        from uptox.services.tick_handler import on_tick
        await run_feed(on_tick)
