# Entry Candidate Scoring — Design Spec

Date: 2026-07-23

## Goal

Today's live entry pipeline (`dashboard.py`'s per-tick handler) evaluates six
detector families in a fixed sequence — `sustained`, `retest`, `momentum`,
`reentry`, `lux_sr.check_entry`, `consolidation_range.check_entry` — using a
hard short-circuit (`if not entry_type:`). Whichever detector happens to be
checked first when multiple would fire on the same tick wins by accident of
code order, not by trade quality. This surfaced concretely on 2026-07-23: a
`consol_breakdown` signal and the existing `lux_break` signal both would have
fired around the same window, and only code order decided which traded.

Replace fixed-order short-circuiting with candidate scoring — but scoped much
narrower than first described. Reading the actual code (not just the
conceptual model) during planning showed that `reentry`, `pdh_momentum`,
`range_break`, `pdl_reclaim`/`pdh_loss`, `orb`, `pdh_break`/`pdl_break`,
`sr_retest`, `retest`, `momentum`, and even `sustained` all live inside one
~500-line stateful watch machine (`self._watch`, `self._orb_watching`,
`self._pdx_watching`, `self._sr_retest_mode`, nested if/elif/else branches)
that already self-serializes them — only one of them can ever produce a
result on a given tick, by construction, today. Untangling that machine to
make each sub-check independently competitive would be a large, risky rewrite
of the most delicate part of the live trading engine, for a collision that
was never actually possible between those sub-checks in the first place.

The two checks that ARE genuinely independent and already short-circuited
against each other are `lux_sr.check_entry` and
`consolidation_range.check_entry` — both run unconditionally after the whole
watch machine, both gated only by `if not entry_type:`. That's exactly the
2026-07-23 collision (`lux_break` vs `consol_breakdown`). Scope: leave the
entire watch machine (including `sustained`) untouched internally — treat
whatever single result it produces as one candidate, scored at zero distance
from its trigger (it already self-validated its own departure/confirmation
before deciding, so it's fairly treated as "fresh"). Remove the short-circuit
only between that one legacy result, `lux_sr`, and `consolidation_range`, and
score among those.

## Non-goals

- No change to position concurrency — the system stays single-position
  (one open trade at a time), exactly as today. This was explicitly
  decided during brainstorming after considering and rejecting a
  multi-slot concurrent-position design.
- No change to exits, capital sizing formula, the `_FOCUS_ENTRIES`
  whitelist, `_near_sr` proximity blocking, or daily limits
  (`can_trade_today`). All of that still applies afterward, unchanged, to
  whichever entry type wins the candidate selection.
- No change to `lux_sr.py` or `consolidation_range.py` internals — their
  `check_entry()` functions are called exactly as today; only how their
  result is *used* by the caller changes.

## Architecture

```
tick arrives
    │
    ▼
entire legacy watch machine runs EXACTLY as today, untouched internally
(reentry, pdh_momentum, range_break, pdl_reclaim/pdh_loss, orb,
pdh_break/pdl_break, sr_retest, retest, momentum, sustained — all still
self-serialize via self._watch/self._orb_watching/self._pdx_watching/
self._sr_retest_mode exactly as today)
    │
    ▼
legacy_entry_type = entry_type   (whatever the machine decided, or None)
entry_type reset to None so the next two checks still run this tick
    │
    ▼
lux_sr.check_entry(option)              → always runs now (was short-circuited)
consolidation_range.check_entry(option) → always runs now (was short-circuited)
    │
    ▼
candidates: list[Candidate]   (0, 1, 2, or 3 — legacy + lux + consol)
    │
    ├─ 0 candidates  → entry_type = None (today's no-signal case, unchanged)
    ├─ 1 candidate   → entry_type = that one (today's normal case, unchanged)
    └─ 2+ candidates → _pick_best_candidate(candidates) → entry_type
    │
    ▼
entry_type flows into the EXISTING pipeline, untouched:
    focus-mode whitelist → _near_sr block → can_trade_today() →
    strike/capital viability → risk sizing → open trade
```

## Components

All changes live in `src/uptox/ui/screens/dashboard.py`; no new files, and
no changes to `lux_sr.py` or `consolidation_range.py` (their `_event[2]`
module attribute — already `(slot, event_name, level)` — is read directly
for the trigger level, same cross-module-introspection pattern this file
already uses for `trailing_sl_service.get_status()`; no new return value
needed from either module).

**`Candidate` shape** (a plain dict, not a new class — matches this file's
existing convention of passing dicts/tuples rather than dataclasses):
`{"entry_type": str, "option": "CE"|"PE", "trigger_level": float}`.

**Only two `if not entry_type:` guards are removed** — the ones around
`lux_sr.check_entry` and `consolidation_range.check_entry`. The entire
legacy watch machine (reentry through sustained) is untouched, runs exactly
as today, and its result (if any) is captured once, at the end, into
`legacy_entry_type` before `entry_type` is reset to `None` so the two
module checks still run regardless of what the legacy machine decided.

**Trigger-level reference per candidate:**

| Candidate source | `trigger_level` |
|---|---|
| Legacy watch machine's result (whatever it is) | `ltp` itself — distance term is `0` |
| `lux_break`/`lux_bounce`/`lux_retest` | `lux_sr._event[2]` (the level it fired against) |
| `consol_break`/`consol_breakdown` | `consolidation_range._event[2]` (the box edge it fired against) |

**The legacy result scores at zero distance from its trigger — not because
it lacks a level, but because it already validated its own departure/
confirmation internally before deciding** (its own departure thresholds,
retest-bar counts, or sustained's 4-bar confirmation already did that job).
Re-deriving which of its ~10 possible entry types fired and reaching into
each one's specific internal level variable would require touching the
watch machine's internals — exactly what this design avoids. Treating it
as "fresh" is honest: by the time it reports a result, it has already
earned it under its own rules.

**`_pick_best_candidate(candidates: list[dict], ltp: float, atr: float) ->
dict | None`** — new pure-ish function (calls `capital_service.levels()`,
matching what the file already does downstream today, just earlier):

```python
def _pick_best_candidate(candidates, ltp, atr, delta=None, iv=0.0):
    scored = []
    for c in candidates:
        # same call already made downstream today (capital_service.py:305)
        lvl = capital_service.levels(ltp, delta, iv=iv)
        sl, target = lvl["sl"], lvl["target"]
        risk = abs(ltp - sl)
        reward = abs(target - ltp)
        if risk <= 0:
            continue  # matches existing 0-lots reject downstream
        rr = reward / risk
        if rr < 1.0:  # matches existing "Achieved RR" floor already tracked in Performance panel
            continue
        distance = abs(ltp - c["trigger_level"])  # 0 for the legacy candidate (trigger_level == ltp)
        score = rr - (distance / atr if atr else distance)
        scored.append((score, c))
    if not scored:
        return None
    scored.sort(key=lambda x: x[0], reverse=True)
    return scored[0][1]
```

`capital_service.levels(buy_price, delta=None, iv=0.0, repeat_count=0) -> dict`
(real signature, `src/uptox/services/capital_service.py:305`) returns
`{"sl", "target", "sl_pts", "tgt_pts", "regime"}` — the implementation task
wires `delta`/`iv` from the same live tick values the existing downstream
sizing call already uses at this point in the file, and passes
`repeat_count=trade_book.same_direction_streak(option)` matching the
existing call's convention.

## Data flow

1. Tick arrives, all six detectors evaluate unconditionally.
2. Each that fires appends a candidate; watches/counters advance regardless.
3. If `len(candidates) >= 2`, call `_pick_best_candidate`; else pass through.
4. Chosen `entry_type` (or `None`) proceeds into the existing pipeline
   exactly as today — whitelist, S/R block, daily limits, sizing, open.

## Error handling

If `capital_service.levels()` raises for a candidate during scoring (bad
delta/IV data), that candidate is dropped and logged — not fatal. If ALL
candidates fail scoring, `entry_type` falls back to `None` (no trade this
tick), same as today's "no valid entry" outcome. This mirrors the existing
0-lots / no-viable-strike handling already in this file.

## Testing

`_pick_best_candidate` is pure enough to unit-test directly with synthetic
candidate lists and a stubbed `capital_service.levels`:

- Two candidates, one far from its level with good R:R vs one fresh with
  mediocre R:R — assert the combined score picks correctly in each
  direction (not just "closest always wins").
- Single candidate — passthrough, no scoring call needed.
- Empty candidate list — returns `None`.
- All candidates fail the R:R floor — returns `None` (no trade), not a
  crash.
- Legacy-result candidate (trigger_level == ltp, distance 0) alongside a
  `lux_sr`/`consolidation_range` candidate with a real trigger level —
  confirm the zero-distance treatment doesn't unfairly advantage or
  disadvantage it relative to a fairly-scored level-anchored candidate.

## Open item for later (explicitly out of scope now)

- Tuning the exact score formula weighting (how much R:R matters vs
  freshness) will need real observation once this is live — the formula
  above is a reasonable starting point, not a backtested-optimal one.
