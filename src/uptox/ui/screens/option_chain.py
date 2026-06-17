from textual.app import ComposeResult
from textual.widgets import Static, DataTable, Label, Select
from textual.containers import Horizontal, Vertical


EXPIRIES = ["27-Jun-2026", "03-Jul-2026", "10-Jul-2026", "25-Jul-2026", "28-Aug-2026"]

CHAIN_DATA = [
    # call_oi, call_chg, call_ltp, call_iv, strike, put_iv, put_ltp, put_chg, put_oi
    ("2,12,500", "+18,900", "320.50", "18.5%", "24200", "16.2%", "28.40",  "-4,200", "89,500"),
    ("1,89,000", "+9,600",  "245.30", "17.9%", "24300", "16.8%", "42.60",  "-5,100", "1,02,300"),
    ("1,56,000", "+12,400", "178.80", "17.2%", "24400", "17.1%", "65.20",  "-6,800", "1,24,500"),
    ("2,45,600", "+21,200", "125.50", "16.8%", "24500", "17.5%", "98.40",  "-9,200", "1,98,700"),  # ATM
    ("1,45,200", "+6,800",  "84.20",  "17.1%", "24600", "18.0%", "142.30", "-7,400", "1,56,200"),
    ("1,02,400", "+3,200",  "52.60",  "17.6%", "24700", "18.6%", "198.70", "-5,600", "98,400"),
    ("78,500",   "+1,900",  "30.80",  "18.2%", "24800", "19.2%", "265.40", "-3,800", "72,100"),
    ("54,200",   "+800",    "16.40",  "19.0%", "24900", "20.1%", "340.20", "-2,100", "48,600"),
    ("38,100",   "+400",    "8.20",   "19.8%", "25000", "21.0%", "420.80", "-1,400", "32,900"),
]


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
    OptionChainScreen .header-item {
        margin-right: 3;
        color: $text-muted;
    }
    OptionChainScreen .header-value {
        color: $accent;
        text-style: bold;
        margin-right: 3;
    }
    OptionChainScreen .spot-value {
        color: $success;
        text-style: bold;
        margin-right: 3;
    }
    OptionChainScreen .atm-label {
        color: $warning;
        text-style: bold;
    }
    """

    def compose(self) -> ComposeResult:
        with Horizontal(classes="chain-header"):
            yield Label("Symbol:", classes="header-item")
            yield Label("NIFTY 50", classes="header-value")
            yield Label("Spot:", classes="header-item")
            yield Label("24,512.30", classes="spot-value")
            yield Label("Expiry:", classes="header-item")
            yield Select(
                [(e, e) for e in EXPIRIES],
                value=EXPIRIES[0],
                id="expiry-select",
            )
        yield DataTable(id="chain-table")

    def on_mount(self) -> None:
        table = self.query_one("#chain-table", DataTable)
        table.add_columns(
            "OI (C)", "OI Chg", "LTP (C)", "IV (C)",
            "Strike",
            "IV (P)", "LTP (P)", "OI Chg", "OI (P)",
        )
        atm_strike = "24500"
        for row in CHAIN_DATA:
            call_oi, call_chg, call_ltp, call_iv, strike, put_iv, put_ltp, put_chg, put_oi = row
            label = f"★ {strike} ATM" if strike == atm_strike else strike
            table.add_row(call_oi, call_chg, call_ltp, call_iv, label, put_iv, put_ltp, put_chg, put_oi)
