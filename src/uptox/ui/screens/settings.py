"""
Notification settings screen — toggle each Telegram alert type on/off,
and tune sub-settings (intervals, thresholds) without touching code.
"""
from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import ScrollableContainer, Horizontal, Vertical
from textual.widgets import Static, Switch, Input, Label, Button

from uptox.services import notification_settings as ns

_NOTIF_LABELS: dict[str, str] = {
    "trade_open":        "Trade Open",
    "trade_close":       "Trade Closed",
    "partial_exit":      "Partial Exit",
    "trade_update":      "Trade Update (periodic)",
    "market_update":     "Market Update (periodic)",
    "daily_report":      "Daily Report (3:30 PM)",
    "weekly_report":     "Weekly Report (last trading day of week)",
    "monthly_report":    "Monthly Report (last trading day of month)",
    "daily_limit_hit":   "Daily Limit Hit",
    "approaching_limit": "Approaching Daily Loss Limit",
    "feed_status":       "Feed Disconnect/Reconnect",
}

_NOTIF_DESC: dict[str, str] = {
    "trade_open":        "Sent the instant a new trade opens — strike, entry, SL, target.",
    "trade_close":       "Sent when a trade closes — P&L, peak, exit reason.",
    "partial_exit":      "Sent when half a multi-lot position closes at target.",
    "trade_update":      "Recurring status ping while a trade is open — entry/now/peak/trail SL/phase.",
    "market_update":     "Recurring market snapshot sent regardless of trade state — trend/regime/PCR/OI.",
    "daily_report":      "End-of-day summary at 3:30 PM — trades, P&L, capital.",
    "weekly_report":     "Aggregated summary sent once, on the week's final trading day at 3:30 PM.",
    "monthly_report":    "Aggregated summary sent once, on the month's final trading day at 3:30 PM.",
    "daily_limit_hit":   "Sent the moment a daily limit (loss/trades/consec-losses) stops trading.",
    "approaching_limit": "Early warning before the hard daily-loss-limit stop triggers.",
    "feed_status":       "Sent when the live WSS feed disconnects, and again when it recovers.",
}

# Sub-settings: key -> (sub_key, display label, unit suffix)
_SUB_SETTINGS: dict[str, tuple[str, str, str]] = {
    "trade_update":      ("interval_min",   "Interval", "min"),
    "market_update":     ("interval_min",   "Interval", "min"),
    "approaching_limit": ("threshold_pct",  "Threshold", "% of limit"),
}


class NotificationRow(Static):
    """One notification type: switch + label + description, with optional sub-setting input."""

    def __init__(self, key: str) -> None:
        super().__init__()
        self.key = key

    def compose(self) -> ComposeResult:
        enabled = ns.is_enabled(self.key)
        with Vertical(classes="notif-row"):
            with Horizontal(classes="notif-header"):
                yield Switch(value=enabled, id=f"sw-{self.key}")
                yield Label(_NOTIF_LABELS[self.key], classes="notif-label")
            yield Label(_NOTIF_DESC[self.key], classes="notif-desc")
            if self.key in _SUB_SETTINGS:
                sub_key, label, unit = _SUB_SETTINGS[self.key]
                value = ns.get_sub(self.key, sub_key)
                bounds = ns.get_bounds(self.key, sub_key)
                is_interval = sub_key == "interval_min"
                none_hint = "  (0 = None / off)" if is_interval else ""
                bound_str = f"  (range {bounds[0]:.0f}-{bounds[1]:.0f})" if bounds else ""
                with Horizontal(classes="notif-sub"):
                    yield Label(f"{label}:", classes="sub-label")
                    yield Input(
                        value=str(value), id=f"in-{self.key}-{sub_key}",
                        classes="sub-input", type="number",
                    )
                    unit_text = "None" if (is_interval and value == 0) else unit
                    yield Label(f"{unit_text}{bound_str}{none_hint}", classes="sub-unit", id=f"unit-{self.key}-{sub_key}")

    def on_switch_changed(self, event: Switch.Changed) -> None:
        ns.set_enabled(self.key, event.value)
        ns.save()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if self.key not in _SUB_SETTINGS:
            return
        sub_key, _, unit = _SUB_SETTINGS[self.key]
        try:
            value = float(event.value) if "." in event.value else int(event.value)
        except ValueError:
            return
        ns.set_sub(self.key, sub_key, value)
        ns.save()
        new_value = ns.get_sub(self.key, sub_key)
        # Reflect any clamping back into the input
        event.input.value = str(new_value)
        # Update the "None" hint live when interval is set to 0
        if sub_key == "interval_min":
            unit_label = self.query_one(f"#unit-{self.key}-{sub_key}", Label)
            bounds = ns.get_bounds(self.key, sub_key)
            bound_str = f"  (range {bounds[0]:.0f}-{bounds[1]:.0f})" if bounds else ""
            unit_text = "None" if new_value == 0 else unit
            unit_label.update(f"{unit_text}{bound_str}  (0 = None / off)")


class SettingsScreen(ScrollableContainer):
    DEFAULT_CSS = """
    SettingsScreen {
        padding: 1 2;
    }
    .notif-row {
        border: round $primary-darken-2;
        padding: 1;
        margin-bottom: 1;
        height: auto;
    }
    .notif-header {
        height: auto;
        align-vertical: middle;
    }
    .notif-label {
        padding-left: 2;
        text-style: bold;
    }
    .notif-desc {
        color: $text-muted;
        padding-left: 6;
    }
    .notif-sub {
        height: auto;
        padding-left: 6;
        margin-top: 1;
        align-vertical: middle;
    }
    .sub-label {
        padding-right: 1;
    }
    .sub-input {
        width: 10;
    }
    .sub-unit {
        padding-left: 1;
        color: $text-muted;
    }
    #settings-title {
        text-style: bold;
        padding-bottom: 1;
    }
    #save-row {
        height: auto;
        margin-top: 1;
        align-horizontal: center;
    }
    """

    def compose(self) -> ComposeResult:
        yield Label("Telegram Notification Settings", id="settings-title")
        for key in _NOTIF_LABELS:
            yield NotificationRow(key)
        with Horizontal(id="save-row"):
            yield Button("Reset to Defaults", id="btn-reset", variant="warning")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-reset":
            ns.reset_all()
            ns.save()
            self.app.notify("Notification settings reset to defaults", severity="information")
            self.refresh(recompose=True)
