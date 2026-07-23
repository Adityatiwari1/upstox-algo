"""_pick_best_candidate: when lux_sr and consolidation_range (and/or the
legacy watch machine's own result) fire on the same tick, pick the best
one by freshness (distance from trigger_level) combined with risk/reward,
instead of whichever was checked first in code order. Pure function --
capital_service.levels is monkeypatched, matching this codebase's existing
test convention (no pytest, direct module attribute reassignment)."""
from uptox.ui.screens import dashboard
from uptox.services import capital_service


def _stub_levels(sl, target):
    def _fn(buy_price, delta=None, iv=0.0, repeat_count=0):
        return {"sl": sl, "target": target, "sl_pts": None, "tgt_pts": None, "regime": "NORMAL"}
    return _fn


def test_empty_candidates_returns_none():
    assert dashboard._pick_best_candidate([], ltp=24200.0, atr=20.0) is None


def test_single_candidate_passthrough():
    capital_service.levels = _stub_levels(sl=24190.0, target=24220.0)
    candidates = [{"entry_type": "lux_break", "option": "CE", "trigger_level": 24195.0}]
    assert dashboard._pick_best_candidate(candidates, ltp=24200.0, atr=20.0) == "lux_break"


def test_fresh_break_beats_stale_break_at_similar_rr():
    capital_service.levels = _stub_levels(sl=24190.0, target=24220.0)  # same R:R for both
    candidates = [
        {"entry_type": "lux_break", "option": "CE", "trigger_level": 24195.0},    # 5pt away -- fresh
        {"entry_type": "consol_break", "option": "CE", "trigger_level": 24170.0},  # 30pt away -- stale
    ]
    assert dashboard._pick_best_candidate(candidates, ltp=24200.0, atr=20.0) == "lux_break"


def test_better_rr_beats_fresher_but_worse_rr():
    # lux_break: fresh (2pt away) but terrible R:R (sl very close, target very close)
    # consol_break: further away (25pt) but a much better R:R
    calls = {"n": 0}

    def _fn(buy_price, delta=None, iv=0.0, repeat_count=0):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"sl": 24197.0, "target": 24203.0, "sl_pts": None, "tgt_pts": None, "regime": "NORMAL"}  # rr=1.0
        return {"sl": 24150.0, "target": 24350.0, "sl_pts": None, "tgt_pts": None, "regime": "NORMAL"}      # rr huge

    capital_service.levels = _fn
    candidates = [
        {"entry_type": "lux_break", "option": "CE", "trigger_level": 24198.0},
        {"entry_type": "consol_break", "option": "CE", "trigger_level": 24175.0},
    ]
    assert dashboard._pick_best_candidate(candidates, ltp=24200.0, atr=20.0) == "consol_break"


def test_legacy_candidate_scores_at_zero_distance():
    capital_service.levels = _stub_levels(sl=24190.0, target=24220.0)  # same R:R for both
    candidates = [
        {"entry_type": "sustained", "option": "CE", "trigger_level": 24200.0},   # legacy: trigger_level == ltp
        {"entry_type": "lux_break", "option": "CE", "trigger_level": 24150.0},   # 50pt away
    ]
    assert dashboard._pick_best_candidate(candidates, ltp=24200.0, atr=20.0) == "sustained"


def test_all_candidates_fail_rr_floor_returns_none():
    capital_service.levels = _stub_levels(sl=24199.0, target=24200.5)  # rr < 1.0
    candidates = [{"entry_type": "lux_break", "option": "CE", "trigger_level": 24198.0}]
    assert dashboard._pick_best_candidate(candidates, ltp=24200.0, atr=20.0) is None


def test_zero_risk_candidate_is_dropped_not_crashed():
    capital_service.levels = _stub_levels(sl=24200.0, target=24220.0)  # sl == ltp -> risk 0
    candidates = [{"entry_type": "lux_break", "option": "CE", "trigger_level": 24198.0}]
    assert dashboard._pick_best_candidate(candidates, ltp=24200.0, atr=20.0) is None


def test_levels_exception_drops_that_candidate_not_the_whole_call():
    calls = {"n": 0}

    def _fn(buy_price, delta=None, iv=0.0, repeat_count=0):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom -- bad iv data")
        return {"sl": 24190.0, "target": 24220.0, "sl_pts": None, "tgt_pts": None, "regime": "NORMAL"}

    capital_service.levels = _fn
    candidates = [
        {"entry_type": "lux_break", "option": "CE", "trigger_level": 24198.0},   # raises
        {"entry_type": "consol_break", "option": "CE", "trigger_level": 24175.0},  # survives
    ]
    assert dashboard._pick_best_candidate(candidates, ltp=24200.0, atr=20.0) == "consol_break"


def test_all_candidates_raise_returns_none_not_crash():
    def _fn(buy_price, delta=None, iv=0.0, repeat_count=0):
        raise RuntimeError("boom")

    capital_service.levels = _fn
    candidates = [{"entry_type": "lux_break", "option": "CE", "trigger_level": 24198.0}]
    assert dashboard._pick_best_candidate(candidates, ltp=24200.0, atr=20.0) is None
