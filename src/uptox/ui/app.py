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
        ("t", "cycle_tf", "Cycle TF"),
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
            self.run_worker(self._prime_smas(), name="sma-prime")
            self.run_worker(self._prime_option_chain(), name="oc-prime")

    async def _start_feed(self) -> None:
        from uptox.api.feed_client import run_feed
        from uptox.services.tick_handler import on_tick
        await run_feed(on_tick)

    def action_cycle_tf(self) -> None:
        try:
            from uptox.ui.screens.dashboard import SMAPanel
            self.query_one(SMAPanel).cycle_tf()
        except Exception:
            pass

    async def _prime_smas(self) -> None:
        from uptox.api.feed_client import DEFAULT_INSTRUMENTS
        from uptox.services.sma_service import fetch_and_prime
        import asyncio
        await asyncio.gather(*(
            fetch_and_prime(key, settings.access_token)
            for key in DEFAULT_INSTRUMENTS
        ))

    async def _prime_option_chain(self) -> None:
        from uptox.services.option_chain_service import fetch_and_prime, get_keys_for_atm
        from uptox.services import feed_store
        from uptox.api.feed_client import subscribe
        await fetch_and_prime(settings.access_token)
        # Subscribe ATM ±5 immediately using current LTP (if feed already running)
        t = feed_store.get("NSE_INDEX|Nifty 50")
        if t and t.ltp:
            keys = get_keys_for_atm(t.ltp, n=5)
            if keys:
                await subscribe(keys)
