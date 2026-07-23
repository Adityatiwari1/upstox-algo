# Entry Candidate Scoring Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When `lux_sr.check_entry` and `consolidation_range.check_entry` both fire on the same tick (today, whichever is checked first silently wins), score both against whatever the legacy watch-state machine decided and take the best one by freshness + risk/reward, instead of code-order luck.

**Architecture:** The legacy watch machine (reentry, pdh_momentum, range_break, pdl_reclaim/pdh_loss, orb, pdh_break/pdl_break, sr_retest, retest, momentum, sustained — all self-serialized via `self._watch`/`self._orb_watching`/`self._pdx_watching`/`self._sr_retest_mode`) is untouched internally. Its single result (if any) is captured once, then `lux_sr.check_entry` and `consolidation_range.check_entry` — the only two checks that are genuinely independent and were short-circuited against each other — both run unconditionally. Whatever subset of {legacy result, lux result, consol result} fired gets scored by a new pure function; the winner becomes `entry_type` and flows into the existing downstream pipeline (whitelist, S/R block, daily limits, sizing) completely unchanged.

**Tech Stack:** Python 3.11+, no new dependencies. Same `uv run python -c "..."` inline test runner used throughout this project (no pytest installed).

## Global Constraints

- No pytest installed in this venv — every test in this plan is a plain `assert`-based function, run via the inline runner shown in Task 1.
- Do not modify `lux_sr.py` or `consolidation_range.py` — their trigger level is read via the existing module-level `_event` attribute (`(slot, event_name, level)`, already present in both modules), the same cross-module-introspection pattern this codebase already uses for `trailing_sl_service.get_status()`.
- Do not modify anything inside the legacy watch machine (`src/uptox/ui/screens/dashboard.py` lines ~954-1372: reentry through sustained). It must produce exactly the same result it does today, for the same inputs.
- Everything downstream of entry-type selection (focus-mode whitelist, `_near_sr`, `can_trade_today()`, strike/capital viability, risk sizing) is untouched — it still receives one `entry_type` string and behaves exactly as today.
- `logs/` data files are tracked in git and the user commits them manually — never `git add`/`git commit` any path under `logs/` in this plan's steps.

---

## File Structure

```
src/uptox/ui/screens/dashboard.py
    - Add: _pick_best_candidate() module-level function (near _near_sr, ~line 252)
    - Modify: lines 1374-1392 (the lux_sr / consolidation_range check block)

tests/
    - Create: tests/test_pick_best_candidate.py
```

---

### Task 1: `_pick_best_candidate` — pure scoring function

**Files:**
- Modify: `src/uptox/ui/screens/dashboard.py` (insert after line 251, before `def _sane_tick`)
- Test: `tests/test_pick_best_candidate.py`

**Interfaces:**
- Consumes: `capital_service.levels(buy_price, delta=None, iv=0.0, repeat_count=0) -> dict` (existing, `src/uptox/services/capital_service.py:305`, returns `{"sl", "target", "sl_pts", "tgt_pts", "regime"}`).
- Produces: `_pick_best_candidate(candidates: list[dict], ltp: float, atr: float, delta: float | None = None, iv: float = 0.0, repeat_count: int = 0) -> str | None`. Each candidate dict has keys `"entry_type"` (str), `"option"` ("CE"|"PE"), `"trigger_level"` (float). Returns the winning candidate's `"entry_type"`, or `None` if the list is empty or every candidate fails the R:R floor. Task 2 calls this directly.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_pick_best_candidate.py
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
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run python -c "
import sys; sys.path.insert(0, 'tests'); sys.path.insert(0, 'src')
import test_pick_best_candidate as t
t.test_empty_candidates_returns_none()
"
```
Expected: `AttributeError: module 'uptox.ui.screens.dashboard' has no attribute '_pick_best_candidate'`

- [ ] **Step 3: Write minimal implementation**

Insert into `src/uptox/ui/screens/dashboard.py` immediately after line 251 (`return None, 0.0`, the end of `_near_sr`) and before `def _sane_tick(...)`:

```python
def _pick_best_candidate(
    candidates: list[dict], ltp: float, atr: float,
    delta: float | None = None, iv: float = 0.0, repeat_count: int = 0,
) -> str | None:
    """When 2+ entry types fire on the same tick (2026-07-23: lux_break vs
    consol_breakdown), pick the best by combining freshness (distance from
    the candidate's own trigger level -- a break that's already run far
    past its level is a worse entry than one that just crossed it) with
    risk/reward (via the same capital_service.levels() call the downstream
    sizing step already makes). The legacy watch machine's own result (if
    any) is passed in with trigger_level == ltp, scoring at zero distance --
    it already validated its own departure/confirmation internally before
    deciding, so it's fairly judged on R:R alone, same as every other
    candidate here.

    See docs/superpowers/specs/2026-07-23-entry-candidate-scoring-design.md.
    """
    if not candidates:
        return None
    scored: list[tuple[float, str]] = []
    for c in candidates:
        try:
            lvl = capital_service.levels(ltp, delta, iv=iv, repeat_count=repeat_count)
        except Exception as e:
            log.warning("Entry candidate scoring: levels() failed for %s, dropping candidate: %s",
                        c["entry_type"], e)
            continue
        sl, target = lvl["sl"], lvl["target"]
        risk = abs(ltp - sl)
        reward = abs(target - ltp)
        if risk <= 0:
            continue
        rr = reward / risk
        if rr < 1.0:
            continue
        distance = abs(ltp - c["trigger_level"])
        score = rr - (distance / atr if atr else distance)
        scored.append((score, c["entry_type"]))
    if not scored:
        return None
    scored.sort(key=lambda x: x[0], reverse=True)
    return scored[0][1]
```

- [ ] **Step 4: Run test to verify it passes**

```bash
uv run python -c "
import sys; sys.path.insert(0, 'tests'); sys.path.insert(0, 'src')
import test_pick_best_candidate as t
import traceback
fails = 0
for name in dir(t):
    if name.startswith('test_'):
        try:
            getattr(t, name)(); print('PASS', name)
        except Exception as e:
            fails += 1; print('FAIL', name, '->', e); traceback.print_exc()
print('failures:', fails)
"
```
Expected: 9 `PASS` lines, `failures: 0`

- [ ] **Step 5: Commit**

```bash
git add src/uptox/ui/screens/dashboard.py tests/test_pick_best_candidate.py
git commit -m "feat(dashboard): add _pick_best_candidate for entry-type scoring"
```

---

### Task 2: Wire scoring into the tick handler

**Files:**
- Modify: `src/uptox/ui/screens/dashboard.py:1374-1392`

**Interfaces:**
- Consumes: `_pick_best_candidate` (Task 1); `lux_sr._event` and `consolidation_range._event` module attributes (existing, `(slot, event_name, level) | None`); `atr_service.get_atr(instrument_key, timeframe) -> float | None` (existing, already called at line 1011 in the reentry block with the identical `NIFTY, TimeFrame.M5` arguments).
- Produces: the tick handler's `entry_type` variable, now chosen by scoring when 2+ of {legacy result, lux result, consol result} fire on the same tick. No new public interface — this is the integration point, nothing later in the file depends on new names.

- [ ] **Step 1: Read the current block to confirm line numbers haven't shifted**

```bash
uv run python -c "
lines = open('src/uptox/ui/screens/dashboard.py', encoding='utf-8').readlines()
print(''.join(lines[1372:1393]))
"
```
Expected output (confirms nothing has moved since this plan was written):
```python

                    # Lux S/R events (break / bounce / retest at 10/10-wing
                    # pivot levels) — level-anchored, so focus-whitelisted
                    if not entry_type:
                        _lux_et = lux_sr.check_entry(option)
                        if _lux_et:
                            entry_type = _lux_et
                            log.info("Lux S/R entry: %s %s @ %.1f (score %+d)",
                                     _lux_et, option, ltp, total)

                    # Consolidation range break/breakdown (2026-07-23) — added
                    # straight to _FOCUS_ENTRIES at the user's explicit call,
                    # skipping the shadow-observation period every other entry
                    # type here went through first.
                    if not entry_type:
                        _consol_et = consolidation_range.check_entry(option)
                        if _consol_et:
                            entry_type = _consol_et
                            log.info("Consolidation range entry: %s %s @ %.1f (score %+d)",
                                     _consol_et, option, ltp, total)
```
If the printed text differs from this, re-locate the block by searching for `"Lux S/R events"` in the file before proceeding — do not guess at new line numbers.

- [ ] **Step 2: Replace the block**

Replace exactly this text:
```python
                    # Lux S/R events (break / bounce / retest at 10/10-wing
                    # pivot levels) — level-anchored, so focus-whitelisted
                    if not entry_type:
                        _lux_et = lux_sr.check_entry(option)
                        if _lux_et:
                            entry_type = _lux_et
                            log.info("Lux S/R entry: %s %s @ %.1f (score %+d)",
                                     _lux_et, option, ltp, total)

                    # Consolidation range break/breakdown (2026-07-23) — added
                    # straight to _FOCUS_ENTRIES at the user's explicit call,
                    # skipping the shadow-observation period every other entry
                    # type here went through first.
                    if not entry_type:
                        _consol_et = consolidation_range.check_entry(option)
                        if _consol_et:
                            entry_type = _consol_et
                            log.info("Consolidation range entry: %s %s @ %.1f (score %+d)",
                                     _consol_et, option, ltp, total)
```
with:
```python
                    # Entry candidate scoring (2026-07-23): the legacy watch
                    # machine above (reentry through sustained) already
                    # self-serializes -- only one of ITS checks can ever fire
                    # per tick. lux_sr and consolidation_range are the two
                    # genuinely independent checks that were short-circuited
                    # against each other and each other's legacy result --
                    # both now always run, and whichever subset fires gets
                    # scored instead of whichever was checked first. See
                    # docs/superpowers/specs/2026-07-23-entry-candidate-scoring-design.md
                    legacy_entry_type = entry_type
                    entry_type = None  # reset so both checks below always run this tick

                    _lux_et = lux_sr.check_entry(option)
                    if _lux_et:
                        log.info("Lux S/R entry: %s %s @ %.1f (score %+d)",
                                 _lux_et, option, ltp, total)

                    _consol_et = consolidation_range.check_entry(option)
                    if _consol_et:
                        log.info("Consolidation range entry: %s %s @ %.1f (score %+d)",
                                 _consol_et, option, ltp, total)

                    candidates: list[dict] = []
                    if legacy_entry_type:
                        candidates.append({
                            "entry_type": legacy_entry_type, "option": option,
                            "trigger_level": ltp,  # already self-validated internally
                        })
                    if _lux_et:
                        candidates.append({
                            "entry_type": _lux_et, "option": option,
                            "trigger_level": lux_sr._event[2] if lux_sr._event else ltp,
                        })
                    if _consol_et:
                        candidates.append({
                            "entry_type": _consol_et, "option": option,
                            "trigger_level": consolidation_range._event[2] if consolidation_range._event else ltp,
                        })

                    if len(candidates) > 1:
                        from uptox.services import atr_service
                        _atr_pts = atr_service.get_atr(NIFTY, TimeFrame.M5) or 30.0
                        entry_type = _pick_best_candidate(
                            candidates, ltp, _atr_pts,
                            delta=entry_live.delta if entry_live else None,
                            iv=entry_live.iv if entry_live else 0.0,
                            repeat_count=trade_book.same_direction_streak(option),
                        )
                        if entry_type and len(candidates) > 1:
                            log.info("Entry candidate scoring: %d candidates, picked %s",
                                     len(candidates), entry_type)
                    elif candidates:
                        entry_type = candidates[0]["entry_type"]
```

- [ ] **Step 3: Syntax check**

```bash
uv run python -c "import ast; ast.parse(open('src/uptox/ui/screens/dashboard.py', encoding='utf-8').read())" && echo "SYNTAX OK"
```
Expected: `SYNTAX OK`

- [ ] **Step 4: Import smoke check**

```bash
uv run python -c "
import sys; sys.path.insert(0, 'src')
from uptox.ui.screens import dashboard
print('OK -- dashboard imports cleanly')
"
```
Expected: `OK -- dashboard imports cleanly`

- [ ] **Step 5: Re-run Task 1's tests to confirm no regression**

```bash
uv run python -c "
import sys; sys.path.insert(0, 'tests'); sys.path.insert(0, 'src')
import test_pick_best_candidate as t
fails = 0
for name in dir(t):
    if name.startswith('test_'):
        try:
            getattr(t, name)()
        except Exception as e:
            fails += 1; print('FAIL', name, '->', e)
print('failures:', fails)
"
```
Expected: `failures: 0`

- [ ] **Step 6: Manual verification — run the app and watch for the new log line**

```bash
uv run src/uptox/main.py
```
Watch `logs/uptox.log` during a live session for `"Entry candidate scoring: N candidates, picked ..."` on any tick where both `lux_sr` and `consolidation_range` (or one of them plus the legacy machine) fire together. This can't be triggered on demand in a unit test — it depends on live market conditions producing a genuine multi-candidate tick — so this step is manual observation, not an automated check. Report as manually verified once seen, or note that no multi-candidate tick occurred during the observation window if none did.

- [ ] **Step 7: Commit**

```bash
git add src/uptox/ui/screens/dashboard.py
git commit -m "feat(dashboard): score lux_sr/consolidation_range/legacy entry candidates instead of first-match-wins"
```

---

## Post-plan: what's still deliberately out of scope

- The legacy watch machine's ~10 sub-checks (reentry, pdh_momentum, range_break, pdl_reclaim, orb, pdh_break/pdl_break, sr_retest, retest, momentum, sustained) remain fully serialized against each other, exactly as today — untangling them was considered and explicitly rejected as too large/risky a change to the live trading engine for a collision that structurally can't happen between them.
- The score formula's exact weighting (`rr - distance/atr`) is a reasonable starting point, not backtested-optimal — tune once there's real observation of how often multi-candidate ticks actually occur and which side of the tradeoff matters more in practice.
