"""QuantEngine: sizing, hard-stop, profit-lock, target, session-close, the
paper/live safety fork, the market-open entry gate, and the ATM-roll force
close. order_service has no internal gate of its own (verified against
order_service.py) so this engine's mode check is the only thing between a
signal and a real order — test_paper_mode_never_calls_order_service is the
test for that specific invariant.

All snapshots that expect to open a position set an explicit in-hours "ts"
(bar time, not wall clock — see _bar_time) so these tests are deterministic
regardless of when they're actually run; the market-open gate would
otherwise make them depend on real wall-clock time."""
import tempfile
from pathlib import Path

from uptox.quant.strategy import Signal, Strategy
from uptox.quant.quant_trading_mode import QuantTradingMode
from uptox.quant.engine import QuantEngine, LOT_SIZE
from uptox.quant import book

_TS1 = "2026-07-20T09:20:00+05:30"
_TS2 = "2026-07-20T09:25:00+05:30"
_TS3 = "2026-07-20T09:30:00+05:30"


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
    eng.on_snapshot({"ts": _TS1, "nifty": 24200, "ce_ltp": 150.0, "pe_ltp": 80.0}, [])
    assert eng.position is not None
    assert eng.position["option"] == "CE"
    assert eng.position["entry_price"] == 150.0
    rows = book.read_events(path)
    assert rows[0]["event"] == "open"


def test_does_not_open_second_position_while_one_open():
    eng, path, mode = _engine()
    eng.on_snapshot({"ts": _TS1, "nifty": 24200, "ce_ltp": 150.0, "pe_ltp": 80.0}, [])
    first_id = eng.position["id"]
    eng.on_snapshot({"ts": _TS2, "nifty": 24200, "ce_ltp": 151.0, "pe_ltp": 80.0}, [])
    assert eng.position["id"] == first_id


def test_hard_stop_closes_at_minus_10pts():
    eng, path, mode = _engine()
    eng.on_snapshot({"ts": _TS1, "nifty": 24200, "ce_ltp": 150.0, "pe_ltp": 80.0}, [])
    eng.on_snapshot({"ts": _TS2, "nifty": 24200, "ce_ltp": 139.9, "pe_ltp": 80.0}, [])
    assert eng.position is None
    rows = book.read_events(path)
    assert rows[-1]["event"] == "close"
    assert rows[-1]["exit_reason"] == "stop"


def test_profit_lock_closes_after_peak_pullback():
    eng, path, mode = _engine()
    eng.on_snapshot({"ts": _TS1, "nifty": 24200, "ce_ltp": 150.0, "pe_ltp": 80.0}, [])
    eng.on_snapshot({"ts": _TS2, "nifty": 24200, "ce_ltp": 161.0, "pe_ltp": 80.0}, [])  # peak +11
    eng.on_snapshot({"ts": _TS3, "nifty": 24200, "ce_ltp": 155.0, "pe_ltp": 80.0}, [])  # below 50%-of-peak floor
    assert eng.position is None
    rows = book.read_events(path)
    assert rows[-1]["exit_reason"] == "profit_lock"


def test_target_closes_at_2x_stop_distance():
    eng, path, mode = _engine()
    eng.on_snapshot({"ts": _TS1, "nifty": 24200, "ce_ltp": 150.0, "pe_ltp": 80.0}, [])
    eng.on_snapshot({"ts": _TS2, "nifty": 24200, "ce_ltp": 170.1, "pe_ltp": 80.0}, [])  # entry+10*RR(2.0)=170
    assert eng.position is None
    rows = book.read_events(path)
    assert rows[-1]["exit_reason"] == "target"


def test_no_signal_means_no_position():
    eng, path, mode = _engine(strategy=_NeverSignals())
    eng.on_snapshot({"ts": _TS1, "nifty": 24200, "ce_ltp": 150.0, "pe_ltp": 80.0}, [])
    assert eng.position is None
    assert book.read_events(path) == []


def test_zero_capital_skips_open_lots_zero():
    eng, path, mode = _engine(capital=0.0)
    eng.on_snapshot({"ts": _TS1, "nifty": 24200, "ce_ltp": 150.0, "pe_ltp": 80.0}, [])
    assert eng.position is None


def test_paper_mode_never_calls_order_service():
    from uptox.services import order_service
    calls = []
    orig_entry, orig_exit = order_service.schedule_entry_with_sl, order_service.schedule_exit
    try:
        order_service.schedule_entry_with_sl = lambda *a, **kw: calls.append(("entry", a, kw))
        order_service.schedule_exit = lambda *a, **kw: calls.append(("exit", a, kw))
        eng, path, mode = _engine()
        assert mode.is_paper()
        eng.on_snapshot({"ts": _TS1, "nifty": 24200, "ce_ltp": 150.0, "pe_ltp": 80.0}, [])
        eng.on_snapshot({"ts": _TS2, "nifty": 24200, "ce_ltp": 139.9, "pe_ltp": 80.0}, [])  # stop out
        assert calls == [], f"order_service must never be called in paper mode, got: {calls}"
    finally:
        order_service.schedule_entry_with_sl = orig_entry
        order_service.schedule_exit = orig_exit


def test_live_mode_calls_order_service_on_open_and_close():
    from uptox.services import order_service, option_chain_service
    calls = []
    orig_entry = order_service.schedule_entry_with_sl
    orig_exit = order_service.schedule_exit
    orig_get_key = option_chain_service.get_key
    try:
        order_service.schedule_entry_with_sl = lambda *a, **kw: calls.append(("entry", a, kw))
        order_service.schedule_exit = lambda *a, **kw: calls.append(("exit", a, kw))
        option_chain_service.get_key = lambda atm, option: "NSE_FO|test_live"

        eng, path, mode = _engine()
        mode._mode = "live"
        assert mode.is_live()

        eng.on_snapshot({"ts": _TS1, "nifty": 24200, "ce_ltp": 150.0, "pe_ltp": 80.0}, [])
        entry_price = eng.position["entry_price"]
        sl = eng.position["sl"]
        lots = eng.position["lots"]

        eng.on_snapshot({"ts": _TS2, "nifty": 24200, "ce_ltp": 139.9, "pe_ltp": 80.0}, [])  # stop out
        assert eng.position is None

        entry_calls = [c for c in calls if c[0] == "entry"]
        exit_calls = [c for c in calls if c[0] == "exit"]
        assert len(entry_calls) == 1, f"expected exactly one entry call, got: {entry_calls}"
        assert len(exit_calls) == 1, f"expected exactly one exit call, got: {exit_calls}"

        _, entry_args, _ = entry_calls[0]
        assert entry_args == ("NSE_FO|test_live", lots * LOT_SIZE, entry_price, sl), \
            f"entry call args mismatch: {entry_args}"

        _, exit_args, _ = exit_calls[0]
        assert exit_args == ("NSE_FO|test_live", lots * LOT_SIZE, 139.9), \
            f"exit call args mismatch: {exit_args}"
    finally:
        order_service.schedule_entry_with_sl = orig_entry
        order_service.schedule_exit = orig_exit
        option_chain_service.get_key = orig_get_key


def test_session_close_flattens_using_bar_ts_not_wall_clock():
    eng, path, mode = _engine()
    eng.on_snapshot({"ts": "2026-07-20T14:00:00+05:30", "nifty": 24200, "ce_ltp": 150.0, "pe_ltp": 80.0}, [])
    eng.on_snapshot({"ts": "2026-07-20T15:25:00+05:30", "nifty": 24200, "ce_ltp": 152.0, "pe_ltp": 80.0}, [])
    assert eng.position is None
    rows = book.read_events(path)
    assert rows[-1]["exit_reason"] == "session_close"


class _RaisesOnGenerate(Strategy):
    strategy_id = "test_engine"

    def generate_signal(self, history):
        raise RuntimeError("boom — bad feature calc")


def test_strategy_exception_is_caught_and_treated_as_no_signal():
    eng, path, mode = _engine(strategy=_RaisesOnGenerate())
    eng.on_snapshot({"ts": _TS1, "nifty": 24200, "ce_ltp": 150.0, "pe_ltp": 80.0}, [])  # must not raise
    assert eng.position is None
    eng.on_snapshot({"ts": _TS2, "nifty": 24200, "ce_ltp": 151.0, "pe_ltp": 80.0}, [])  # still must not raise
    assert eng.position is None


def test_no_entry_before_market_open():
    # F&O doesn't open till 09:15 -- a signal on an 09:00 pre-open/stale
    # snapshot must not open a position (2026-07-23 bug: entered a 24150 CE
    # off a 09:00:13 snapshot).
    eng, path, mode = _engine()
    eng.on_snapshot({"ts": "2026-07-23T09:00:13+05:30", "nifty": 24169.55,
                      "ce_ltp": 86.5, "pe_ltp": 251.25}, [])
    assert eng.position is None
    assert book.read_events(path) == []


def test_entry_at_exactly_market_open_is_allowed():
    eng, path, mode = _engine()
    eng.on_snapshot({"ts": "2026-07-20T09:15:00+05:30", "nifty": 24200,
                      "ce_ltp": 150.0, "pe_ltp": 80.0}, [])
    assert eng.position is not None


def test_atm_roll_force_closes_at_last_known_matching_price():
    # 2026-07-23 bug: entered the 24150 CE at 86.5, then 5 min later Nifty
    # gapped down and ATM rolled to 23900 -- the engine read ce_ltp=200.75
    # (the NEW strike's price) as if it were still the 24150 CE, recording
    # a fake +114.2pt "target hit" that never happened for the held
    # contract. Once ATM no longer matches the entry strike, there is no
    # recorded price for the actual held contract (data_recorder only ever
    # records the current-ATM CE/PE) -- the only honest move is to force-
    # close at the last price we know is genuinely that contract's.
    eng, path, mode = _engine()
    eng.on_snapshot({"ts": "2026-07-23T09:20:00+05:30", "nifty": 24169,
                      "atm": 24150, "ce_ltp": 86.5, "pe_ltp": 80.0}, [])
    eng.on_snapshot({"ts": "2026-07-23T09:25:00+05:30", "nifty": 24160,
                      "atm": 24150, "ce_ltp": 92.0, "pe_ltp": 80.0}, [])  # same strike, price ticks
    eng.on_snapshot({"ts": "2026-07-23T09:30:00+05:30", "nifty": 23910,
                      "atm": 23900, "ce_ltp": 200.75, "pe_ltp": 80.0}, [])  # ATM rolled away
    assert eng.position is None
    rows = book.read_events(path)
    assert rows[-1]["exit_reason"] == "atm_rolled"
    assert rows[-1]["exit_price"] == 92.0  # last price seen for the ACTUAL held strike


def test_atm_roll_guard_is_backward_compatible_with_no_atm_field():
    # Snapshots without an "atm" field (existing tests, older callers) must
    # behave exactly as before -- the roll guard only activates when both
    # sides of the comparison are actually present.
    eng, path, mode = _engine()
    eng.on_snapshot({"ts": _TS1, "nifty": 24200, "ce_ltp": 150.0, "pe_ltp": 80.0}, [])
    eng.on_snapshot({"ts": _TS2, "nifty": 24200, "ce_ltp": 139.9, "pe_ltp": 80.0}, [])
    assert eng.position is None
    rows = book.read_events(path)
    assert rows[-1]["exit_reason"] == "stop"
