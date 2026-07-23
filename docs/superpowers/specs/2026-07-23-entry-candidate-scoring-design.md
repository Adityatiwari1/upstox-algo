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

Replace fixed-order short-circuiting with candidate scoring: evaluate every
detector every tick (state advances exactly as today — breakout watches still
arm/expire, SMA bar counters still increment), collect every entry type that
actually fires into a candidate list, and when 2 or more fire on the same
tick, pick the one with the best combination of freshness (distance from its
own trigger level) and risk/reward, instead of whichever was checked first.

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
every detector evaluates, unconditionally (state mutation happens
regardless of whether an earlier detector already matched):
    sustained  →  candidate?
    retest     →  candidate?
    momentum   →  candidate?
    reentry    →  candidate?
    lux_sr.check_entry(option)              → candidate?
    consolidation_range.check_entry(option) → candidate?
    │
    ▼
candidates: list[Candidate]   (0, 1, or several)
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

All changes live in `src/uptox/ui/screens/dashboard.py`; no new files.

**`Candidate` shape** (a plain dict, not a new class — matches this file's
existing convention of passing dicts/tuples rather than dataclasses):
`{"entry_type": str, "option": "CE"|"PE", "trigger_level": float}`.

**Detector blocks stop short-circuiting.** Each of the six blocks currently
reads `if not entry_type: <check>; if match: entry_type = match`. Each
becomes `<check>` (always runs, unconditionally — same internal logic and
state mutation as today) `; if match: candidates.append({...})`. The
`if not entry_type:` guard is removed from these six blocks specifically;
every other gate/block in the file (whitelist, S/R proximity, daily limits,
sizing) is untouched and still runs exactly once, after selection.

**Trigger-level reference per entry type** (needed for the freshness term):

| Entry type | `trigger_level` source |
|---|---|
| `range_break`, `orb`, `pdh_*`, `pdl_*`, `sr_retest` | the specific level each already computes (box edge / opening range / prev-day level) |
| `lux_break`, `lux_bounce`, `lux_retest` | the level `lux_sr.check_entry` fired against (already returned via `_event`'s level field) |
| `consol_break`, `consol_breakdown` | the box edge `consolidation_range.check_entry` fired against |
| `retest`, `momentum` | the breakout watch's armed high/low |
| `reentry` | the prior stop-loss price that armed the reentry window |
| `sustained` | none — see below |

**`sustained` has no natural level.** It's a trend-confirmation signal (SMA
score maxed for 4+ bars), not a level-break — forcing a level onto it would
be arbitrary. Its freshness term is fixed at `0` (best-case, no penalty) so
it's judged on R:R alone, competing fairly with level-anchored candidates
rather than being structurally disadvantaged for something that doesn't
apply to it.

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
        distance = abs(ltp - c["trigger_level"]) if c["entry_type"] != "sustained" else 0.0
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
- `sustained` candidate alongside a level-anchored one — confirm the
  zero-distance treatment doesn't unfairly advantage or disadvantage it
  relative to a fairly-scored level-anchored candidate.

## Open item for later (explicitly out of scope now)

- Tuning the exact score formula weighting (how much R:R matters vs
  freshness) will need real observation once this is live — the formula
  above is a reasonable starting point, not a backtested-optimal one.
