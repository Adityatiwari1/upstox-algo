from textual.app import ComposeResult
from textual.widgets import DataTable, Label
from textual.containers import Vertical, ScrollableContainer


class OrdersScreen(ScrollableContainer):
    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("Orders", id="title")
            yield DataTable(id="orders-table")

    def on_mount(self) -> None:
        table = self.query_one("#orders-table", DataTable)
        table.add_columns("Order ID", "Symbol", "Type", "Qty", "Price", "Status")
