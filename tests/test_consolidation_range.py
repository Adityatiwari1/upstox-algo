"""Consolidation Range breakout/breakdown: box tightness measurement,
departure confirmation, and the one-trade-per-box guard. Ported from the
user's Pine Script "Consolidation Range Detector" indicator -- complements
range_break (dashboard.py, 3-bar/25pt/7pt) with a wider 10-bar/%-tight/
15pt version for slower consolidations range_break's short window can't
hold onto long enough to confirm (2026-07-23)."""
import time as _t

from uptox.services import consolidation_range as cr


def _flat_bars(n, px=24200.0, spread=2.0):
    return [(px, px + spread, px - spread, px) for _ in range(n)]


def _reset():
    cr._slot = 0
    cr._in_consolidation = False
    cr._bar_count = 0
    cr._box_high = 0.0
    cr._box_low = 0.0
    cr._confirmed = False
    cr._watch = None
    cr._event = None
    cr._traded = {}
    cr._traded_date = ""


def test_box_range_needs_full_lookback():
    bars = _flat_bars(9)
    assert cr.box_range(bars, lookback=10) is None


def test_box_range_returns_high_low_of_window():
    bars = _flat_bars(10, px=24200.0, spread=3.0)
    box = cr.box_range(bars, lookback=10)
    assert box == (24203.0, 24197.0)


def test_box_range_uses_only_the_last_lookback_bars():
    # 5 wild bars, then 10 tight ones -- only the tight 10 should count
    wild = [(24000.0, 24500.0, 23500.0, 24000.0) for _ in range(5)]
    tight = _flat_bars(10, px=24200.0, spread=2.0)
    bars = wild + tight
    box = cr.box_range(bars, lookback=10)
    assert box == (24202.0, 24198.0)


def test_is_tight_within_threshold():
    # (24210-24190)/24190*100 = 0.0827% -- well under the 0.5% default
    assert cr.is_tight(24210.0, 24190.0) is True


def test_is_tight_exceeds_threshold():
    # (24400-24000)/24000*100 = 1.67% -- over the 0.5% default
    assert cr.is_tight(24400.0, 24000.0) is False


def test_is_tight_zero_low_is_false_not_a_crash():
    assert cr.is_tight(100.0, 0.0) is False


def test_check_live_fires_break_on_departure_confirmed():
    _reset()
    cr._watch = (5, 24200.0, 24150.0)  # armed, box [24150-24200]
    cr.check_live(24216.0)  # 24200 + 16 clears the 15pt departure
    assert cr._watch is None
    assert cr._event is not None
    assert cr._event[1] == "consol_break"
    assert cr._event[2] == 24200.0


def test_check_live_does_not_fire_below_departure_threshold():
    _reset()
    cr._watch = (5, 24200.0, 24150.0)
    cr.check_live(24210.0)  # only 10pt clear, needs 15
    assert cr._watch is not None
    assert cr._event is None


def test_check_live_fires_breakdown_on_departure_confirmed():
    _reset()
    cr._watch = (5, 24200.0, 24150.0)
    cr.check_live(24130.0)  # 24150 - 20 clears the 15pt departure
    assert cr._watch is None
    assert cr._event[1] == "consol_breakdown"
    assert cr._event[2] == 24150.0


def test_check_live_no_op_without_armed_watch():
    _reset()
    cr.check_live(30000.0)  # no watch armed -- nothing to fire
    assert cr._event is None


def test_check_entry_maps_event_to_entry_type():
    _reset()
    slot_now = int(_t.time() // 300)
    cr._event = (slot_now, "consol_break", 24200.0)
    assert cr.check_entry("CE") == "consol_break"


def test_check_entry_wrong_side_returns_none():
    _reset()
    slot_now = int(_t.time() // 300)
    cr._event = (slot_now, "consol_break", 24200.0)
    assert cr.check_entry("PE") is None


def test_check_entry_stale_event_from_a_past_slot_is_rejected():
    _reset()
    cr._event = (1, "consol_break", 24200.0)  # slot 1, long past
    assert cr.check_entry("CE") is None


def test_check_entry_once_per_level_per_day():
    _reset()
    slot_now = int(_t.time() // 300)
    cr._event = (slot_now, "consol_break", 24200.0)
    assert cr.check_entry("CE") == "consol_break"
    cr._event = (slot_now, "consol_break", 24200.0)  # same level, re-fires same slot
    assert cr.check_entry("CE") is None
