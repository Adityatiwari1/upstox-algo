from textual.app import App, ComposeResult
from textual.widgets import Header, Footer, TabbedContent, TabPane
from uptox.ui.screens.dashboard import DashboardScreen
from uptox.ui.screens.orders import OrdersScreen
from uptox.ui.screens.option_chain import OptionChainScreen
from uptox.ui.screens.trade_book import TradeBookScreen
from uptox.ui.screens.settings import SettingsScreen
from uptox.config.settings import settings
from uptox.services import capital_service


class UptoxApp(App):
    TITLE = "Uptox Trading"
    DEFAULT_CSS = """
    TabPane {
        height: 1fr;
        overflow-y: auto;
        overflow-x: hidden;
    }
    """
    BINDINGS = [
        ("q",     "quit",                      "Quit"),
        ("t",     "cycle_tf",                  "Cycle TF"),
        ("]",     "capital_up",                "Capital +5K"),
        ("[",     "capital_down",              "Capital -5K"),
        ("c",     "capital_input",             "Set Capital"),
        ("r",     "cycle_rr",                  "Cycle RR"),
        ("e",     "set_entry",                 "Set Entry"),
        ("x",     "exit_trade",                "Exit Trade"),
        ("R",     "reload_config",             "Reload .env"),
        ("L",     "toggle_trade_mode",         "Toggle Live/Paper"),
    ]

    def compose(self) -> ComposeResult:
        yield Header()
        with TabbedContent(initial="dashboard"):
            with TabPane("Dashboard", id="dashboard"):
                yield DashboardScreen()
            with TabPane("Orders", id="orders"):
                yield OrdersScreen()
            with TabPane("Option Chain", id="option-chain"):
                yield OptionChainScreen()
            with TabPane("Trade Book", id="trade-book"):
                yield TradeBookScreen()
            with TabPane("Settings", id="settings"):
                yield SettingsScreen()
        yield Footer()

    def on_mount(self) -> None:
        from uptox.services.market_calendar import is_trading_day, market_status
        if not is_trading_day():
            self.notify(market_status(), severity="warning", timeout=0)
        if settings.access_token:
            if is_trading_day():
                self.run_worker(self._start_feed(), exclusive=True, name="market-feed")
                self.run_worker(self._prime_smas(), name="sma-prime")
                self.run_worker(self._start_portfolio_feed(), name="portfolio-feed")
            self.run_worker(self._prime_option_chain(), name="oc-prime")
        # Independent of feed/trading day — needs the app process alive, not the market.
        self.run_worker(self._weekly_report_loop(), name="weekly-report-scheduler")

    async def _weekly_report_loop(self) -> None:
        from uptox.services.weekly_report_scheduler import run_forever
        await run_forever()

    async def _start_feed(self) -> None:
        from uptox.api.feed_client import run_feed
        from uptox.services.tick_handler import on_tick
        await run_feed(on_tick)

    async def _start_portfolio_feed(self) -> None:
        from uptox.services.portfolio_feed_service import run_portfolio_feed
        await run_portfolio_feed()

    def action_cycle_tf(self) -> None:
        try:
            from uptox.ui.screens.dashboard import SMAPanel
            self.query_one(SMAPanel).cycle_tf()
        except Exception:
            pass

    def action_capital_up(self) -> None:
        capital_service.set_current_capital(capital_service.get_capital() + 5000)

    def action_capital_down(self) -> None:
        capital_service.set_current_capital(max(5000, capital_service.get_capital() - 5000))

    def action_cycle_rr(self) -> None:
        capital_service.cycle_rr()

    def action_reload_config(self) -> None:
        """Re-read .env in place — no restart. Feed/open trade/SMA state untouched."""
        settings.reload()
        self.notify(
            f"Config reloaded — telegram={'set' if settings.telegram_token else 'empty'}",
            severity="information",
        )

    def action_capital_input(self) -> None:
        try:
            from uptox.ui.screens.dashboard import CapitalPanel
            self.query_one(CapitalPanel).open_input()
        except Exception:
            pass

    def action_set_entry(self) -> None:
        from uptox.services import feed_store, option_chain_service, trailing_sl_service
        from uptox.services import sma_service
        from uptox.services.sma_service import TimeFrame
        import logging
        _log = logging.getLogger(__name__)
        t = feed_store.get("NSE_INDEX|Nifty 50")
        if not t or not t.ltp:
            return
        ltp  = t.ltp
        atm  = round(ltp / 50) * 50
        smas = sma_service.get_smas("NSE_INDEX|Nifty 50", TimeFrame.M5)
        s20, s50 = smas.get(20), smas.get(50)
        sc  = (1 if ltp > s20 else -1 if s20 else 0) + (1 if ltp > s50 else -1 if s50 else 0)
        cum = feed_store.cum_delta("NSE_INDEX|Nifty 50")
        fc  = (1 if t.tbq > t.tsq else -1) + (1 if cum > 0 else -1)
        side = "CE" if (sc + fc) > 0 else "PE" if (sc + fc) < 0 else None
        if not side:
            return
        key  = option_chain_service.get_key(atm, side)
        live = feed_store.get(key) if key else None
        if live and live.ltp:
            trailing_sl_service.set_entry(live.ltp, side, option_key=key)
            _log.info("Entry locked: %s @ %.1f", side, live.ltp)

    def action_exit_trade(self) -> None:
        from uptox.services import feed_store, option_chain_service, trailing_sl_service, trade_book
        ot = trade_book.get_open_trade()
        if ot:
            # Try 1: live tick via stored option_key
            opt_key = ot.get("option_key", "")
            live    = feed_store.get(opt_key) if opt_key else None
            exit_p  = live.ltp if live else 0.0
            # Try 2: look up live tick by strike + option type (handles restored trades with empty key)
            if not exit_p:
                strike_k = option_chain_service.get_key(ot.get("strike", 0), ot.get("option", ""))
                live2    = feed_store.get(strike_k) if strike_k else None
                exit_p   = live2.ltp if live2 else 0.0
            # Try 3: chain REST snapshot ltp
            if not exit_p:
                sn     = option_chain_service.get_option(ot.get("strike", 0), ot.get("option", "")) or {}
                exit_p = float(sn.get("ltp", 0.0))
            # Try 4: last known P&L price (never fall back to entry_price — that gives fake BREAKEVEN)
            if not exit_p:
                exit_p = ot.get("current_price") or ot["entry_price"]
            trade_book.close_trade(exit_p, "manual_exit")
            self.notify(
                f"Trade closed manually — {ot['option']} {ot['strike']} @ ₹{exit_p:.1f}",
                severity="information",
            )
        else:
            trailing_sl_service.exit_trade()

    def action_toggle_trade_mode(self) -> None:
        from uptox.services import trading_mode, trade_book
        # Block mode change while a trade is open — too risky
        if trade_book.has_open_trade():
            self.notify(
                "Cannot switch mode while a trade is open — close trade first",
                severity="error",
                timeout=5,
            )
            return
        new_mode = trading_mode.toggle()
        if new_mode == "live":
            self.notify(
                "⚠  LIVE MODE ACTIVATED — real orders will be placed via Upstox API. "
                "Press L again to return to Paper.",
                severity="error",
                timeout=10,
            )
        else:
            self.notify(
                "Paper mode — no real orders will be placed.",
                severity="information",
                timeout=4,
            )

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
