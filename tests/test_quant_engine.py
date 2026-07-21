"""QuantEngine: sizing, hard-stop, profit-lock, target, session-close, and
the paper/live safety fork. order_service has no internal gate of its own
(verified against order_service.py) so this engine's mode check is the
only thing between a signal and a real order — test_paper_mode_never_calls_order_service
is the test for that specific invariant."""
import tempfile
from pathlib import Path

from uptox.quant.strategy import Signal, Strategy
from uptox.quant.quant_trading_mode import QuantTradingMode
from uptox.quant.engine import QuantEngine
from uptox.quant import book


class _AlwaysCE(Strategy):
    strategy_id = "test_engine"

    def generate_signal(self, history):
        return Signal(option="CE", reason="always", score=1.0, strategy_id=self.strategy_id)


class _NeverSignals(Strategy):
    strategy_id = "test_engine"

    def generate_signal(self, history):
        return None


def _engine(strategy=None, capital=20_000.0):
    path = Path(tempfile.mkstemp(suffix=".jsonl")[1])
    mode = QuantTradingMode("test_engine")
    mode._file = Path(tempfile.mkstemp(suffix=".json")[1])
    mode._mode = "paper"
    return QuantEngine(strategy or _AlwaysCE(), path, mode, capital=capital), path, mode


def test_opens_position_on_signal():
    eng, path, mode = _engine()
    eng.on_snapshot({"nifty": 24200, "ce_ltp": 150.0, "pe_ltp": 80.0}, [])
    assert eng.position is not None
    assert eng.position["option"] == "CE"
    assert eng.position["entry_price"] == 150.0
    rows = book.read_events(path)
    assert rows[0]["event"] == "open"


def test_does_not_open_second_position_while_one_open():
    eng, path, mode = _engine()
    eng.on_snapshot({"nifty": 24200, "ce_ltp": 150.0, "pe_ltp": 80.0}, [])
    first_id = eng.position["id"]
    eng.on_snapshot({"nifty": 24200, "ce_ltp": 151.0, "pe_ltp": 80.0}, [])
    assert eng.position["id"] == first_id


def test_hard_stop_closes_at_minus_10pts():
    eng, path, mode = _engine()
    eng.on_snapshot({"nifty": 24200, "ce_ltp": 150.0, "pe_ltp": 80.0}, [])
    eng.on_snapshot({"nifty": 24200, "ce_ltp": 139.9, "pe_ltp": 80.0}, [])
    assert eng.position is None
    rows = book.read_events(path)
    assert rows[-1]["event"] == "close"
    assert rows[-1]["exit_reason"] == "stop"


def test_profit_lock_closes_after_peak_pullback():
    eng, path, mode = _engine()
    eng.on_snapshot({"nifty": 24200, "ce_ltp": 150.0, "pe_ltp": 80.0}, [])
    eng.on_snapshot({"nifty": 24200, "ce_ltp": 161.0, "pe_ltp": 80.0}, [])  # peak +11
    eng.on_snapshot({"nifty": 24200, "ce_ltp": 155.0, "pe_ltp": 80.0}, [])  # below 50%-of-peak floor
    assert eng.position is None
    rows = book.read_events(path)
    assert rows[-1]["exit_reason"] == "profit_lock"


def test_target_closes_at_2x_stop_distance():
    eng, path, mode = _engine()
    eng.on_snapshot({"nifty": 24200, "ce_ltp": 150.0, "pe_ltp": 80.0}, [])
    eng.on_snapshot({"nifty": 24200, "ce_ltp": 170.1, "pe_ltp": 80.0}, [])  # entry+10*RR(2.0)=170
    assert eng.position is None
    rows = book.read_events(path)
    assert rows[-1]["exit_reason"] == "target"


def test_no_signal_means_no_position():
    eng, path, mode = _engine(strategy=_NeverSignals())
    eng.on_snapshot({"nifty": 24200, "ce_ltp": 150.0, "pe_ltp": 80.0}, [])
    assert eng.position is None
    assert book.read_events(path) == []


def test_zero_capital_skips_open_lots_zero():
    eng, path, mode = _engine(capital=0.0)
    eng.on_snapshot({"nifty": 24200, "ce_ltp": 150.0, "pe_ltp": 80.0}, [])
    assert eng.position is None


def test_paper_mode_never_calls_order_service():
    from uptox.services import order_service
    calls = []
    order_service.schedule_entry_with_sl = lambda *a, **kw: calls.append(("entry", a, kw))
    order_service.schedule_exit = lambda *a, **kw: calls.append(("exit", a, kw))
    eng, path, mode = _engine()
    assert mode.is_paper()
    eng.on_snapshot({"nifty": 24200, "ce_ltp": 150.0, "pe_ltp": 80.0}, [])
    eng.on_snapshot({"nifty": 24200, "ce_ltp": 139.9, "pe_ltp": 80.0}, [])  # stop out
    assert calls == [], f"order_service must never be called in paper mode, got: {calls}"


class _RaisesOnGenerate(Strategy):
    strategy_id = "test_engine"

    def generate_signal(self, history):
        raise RuntimeError("boom — bad feature calc")


def test_strategy_exception_is_caught_and_treated_as_no_signal():
    eng, path, mode = _engine(strategy=_RaisesOnGenerate())
    eng.on_snapshot({"nifty": 24200, "ce_ltp": 150.0, "pe_ltp": 80.0}, [])  # must not raise
    assert eng.position is None
    eng.on_snapshot({"nifty": 24200, "ce_ltp": 151.0, "pe_ltp": 80.0}, [])  # still must not raise
    assert eng.position is None
