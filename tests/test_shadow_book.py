"""Shadow book self-check: a focus-benched entry opens at the real ATM
premium, rides ticks, exits via profit lock or hard stop, and both events
land in the jsonl. No orders, no trade_book writes."""
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace

from uptox.services import shadow_book as sb
from uptox.services import feed_store, option_chain_service, trade_book

KEY = "NSE_FO|test"


def _wire(monkey_ltp, session_close=(24, 0)):
    sb._FILE = Path(tempfile.mkstemp(suffix=".jsonl")[1])
    sb._shadow = None
    sb._cooldown_until = 0.0
    sb._SESSION_CLOSE = session_close
    option_chain_service.get_key = lambda strike, opt: KEY
    feed_store.get = lambda key: SimpleNamespace(ltp=monkey_ltp)
    trade_book.get_open_trade = lambda: None


def test_profit_lock_exit():
    _wire(139.6)
    sb.try_open("sustained", "PE", 24072.8, -5)
    assert sb._shadow and sb._shadow["entry_price"] == 139.6
    sb.on_tick(KEY, 151.0)                   # peak +11.4 -> lock floor 145.3
    assert sb._shadow is not None
    sb.on_tick(KEY, 145.0)                   # below floor -> close
    assert sb._shadow is None
    rows = [json.loads(l) for l in sb._FILE.read_text().splitlines()]
    assert rows[0]["event"] == "open" and rows[1]["event"] == "close"
    assert rows[1]["exit_reason"] == "profit_lock"
    assert rows[1]["pnl_pts"] == 5.4


def test_hard_stop_and_cooldown():
    _wire(100.0)
    sb.try_open("momentum", "CE", 24100.0, 5)
    sb.on_tick(KEY, 89.9)                    # entry-10 breached
    assert sb._shadow is None
    rows = [json.loads(l) for l in sb._FILE.read_text().splitlines()]
    assert rows[1]["exit_reason"] == "stop"
    sb.try_open("momentum", "CE", 24100.0, 5)
    assert sb._shadow is None                # cooldown blocks immediate reopen


def test_no_shadow_while_real_trade_open():
    _wire(100.0)
    trade_book.get_open_trade = lambda: {"id": "real"}
    sb.try_open("sustained", "PE", 24100.0, -5)
    assert sb._shadow is None


def test_session_close_on_any_tick():
    _wire(100.0, session_close=(0, 0))
    sb.try_open("sustained", "PE", 24100.0, -5)
    sb.on_tick("NSE_INDEX|other", 24100.0)   # any instrument triggers it
    assert sb._shadow is None
    rows = [json.loads(l) for l in sb._FILE.read_text().splitlines()]
    assert rows[1]["exit_reason"] == "session_close"


if __name__ == "__main__":
    test_profit_lock_exit()
    test_hard_stop_and_cooldown()
    test_no_shadow_while_real_trade_open()
    test_session_close_on_any_tick()
    print("OK")
