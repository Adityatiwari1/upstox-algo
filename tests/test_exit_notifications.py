"""Regression guard: every trade_book.close_trade() call site must be paired
with a telegram_service.notify_trade_close() call. Two dashboard.py exit
paths (direction_change, signal_flip) used to call close_trade() directly
and never notify — lux_break/bounce/retest entries hit these paths more
often (they skip the S/R re-check and reverse fast), which is why the gap
looked lux-specific even though it affected any open trade."""
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src" / "uptox"


def _close_trade_call_sites(text: str) -> list[int]:
    return [i for i, line in enumerate(text.splitlines())
            if "trade_book.close_trade(" in line or "= close_trade(" in line]


_NOTIFY_MARKERS = ("notify_trade_close", "_reply(", "self.notify(")


def _has_notify_nearby(lines: list[str], idx: int, window: int = 5) -> bool:
    span = lines[idx: idx + window]
    return any(any(m in l for m in _NOTIFY_MARKERS) for l in span)


def test_every_close_trade_call_site_notifies():
    missing = []
    for path in _SRC.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        lines = text.splitlines()
        for idx in _close_trade_call_sites(text):
            if not _has_notify_nearby(lines, idx):
                missing.append(f"{path.relative_to(_SRC)}:{idx + 1}")
    assert not missing, f"close_trade() call sites missing notify_trade_close: {missing}"
