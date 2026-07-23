"""Precondition test for the peek-don't-consume fix in dashboard.py
(commit a7ff4c3 review fix, 2026-07-23): lux_sr.check_entry / consolidation
_range.check_entry mark their level as traded-for-the-day the instant they
match, regardless of whether the caller uses the result. The dashboard fix
avoids calling check_entry() when building scoring candidates (peeking at
_event/_ENTRY_MAP/_traded instead) and only calls the real check_entry() for
the scoring winner. _peek_entry itself is a closure inside the tick handler
and can't be imported, so this test documents and confirms the module-level
side effect the fix depends on: calling check_entry() marks _traded, and
skipping the call (as the fix does for a losing candidate) leaves _traded
untouched so a later genuine occurrence still fires.
No pytest -- plain asserts, matching tests/test_pick_best_candidate.py."""
from datetime import date
import time

from uptox.services import lux_sr, consolidation_range


def _reset(module):
    module._traded = {}
    module._traded_date = date.today().isoformat()
    module._event = None


def test_check_entry_marks_traded_on_match():
    _reset(lux_sr)
    slot = int(time.time() // 300)
    lux_sr._event = (slot, "break_up", 24200.0)
    assert lux_sr.check_entry("CE") == "lux_break"
    assert lux_sr._traded["CE"] == 24200.0


def test_skipped_call_leaves_traded_untouched_bug_precondition():
    # This is the exact scenario from the bug: lux_sr fires this tick but
    # loses the scoring to another candidate. The old code called
    # check_entry() unconditionally to BUILD the candidate list, burning
    # the level even though lux never won. The fix's behavior is to never
    # call check_entry() for a candidate that doesn't win -- simulated
    # here by simply not calling it.
    _reset(lux_sr)
    slot = int(time.time() // 300)
    lux_sr._event = (slot, "break_up", 24200.0)
    # Losing candidate: dashboard must NOT call check_entry() for it.
    # (No call here -- that omission IS the fix.)
    assert lux_sr._traded == {}

    # A later genuine occurrence at the same level, same day, must still
    # be tradeable because nothing marked it traded above.
    assert lux_sr.check_entry("CE") == "lux_break"
    assert lux_sr._traded["CE"] == 24200.0


def test_second_call_same_day_same_level_is_blocked():
    _reset(lux_sr)
    slot = int(time.time() // 300)
    lux_sr._event = (slot, "break_up", 24200.0)
    assert lux_sr.check_entry("CE") == "lux_break"  # winner, real call
    assert lux_sr.check_entry("CE") is None  # already traded today


def test_consolidation_range_shares_the_same_side_effect_shape():
    _reset(consolidation_range)
    slot = int(time.time() // 300)
    consolidation_range._event = (slot, "consol_break", 24300.0)
    assert consolidation_range._traded == {}
    assert consolidation_range.check_entry("CE") == "consol_break"
    assert consolidation_range._traded["CE"] == 24300.0
    assert consolidation_range.check_entry("CE") is None


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
    print("all passed")
