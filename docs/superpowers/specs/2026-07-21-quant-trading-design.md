# Quant Trading — Design Spec

Date: 2026-07-21

## Goal

Build a rule-based, stats-validated quant trading system alongside the existing
focus-mode/velocity-lock system, backtest it on real recorded option-chain data,
then run it live in an isolated paper book so it can be compared against focus
mode before any promotion decision. Judge results by net expectancy, never win
rate alone (per existing [[evaluation-standard]] decision).

Two independent strategies are built and compared, not one:

- **Directional** — predicts NIFTY/BANKNIFTY spot direction (SMA-score +
  momentum), expressed by buying ATM CE/PE.
- **Options dynamics** — trades the option's own behavior (PCR shift, OI
  buildup, IV move), independent of a pure directional call.

Both are intraday-only (flat by session close), matching the current system.
ML is explicitly deferred to a v2 — 20 days of recorded data is too thin to
train/validate a model without high overfit risk; rule-based thresholds swept
and backtested are the right size for this sample.

## Non-goals

- No change to the existing focus-mode/velocity-lock system or its book.
- No real order placement in this phase for either quant strategy — both are
  wired through the real `order_service` code path but forced to paper mode
  via their own independent mode flag (see below). Promotion to live is a
  separate, later, explicit decision — it does not happen as a side effect of
  this build.
- No live wiring of `OptionsDynamicsStrategy` and `DirectionalStrategy` into
  the *same* book, capital pool, or mode flag — they must be independently
  promotable.

## Architecture

```
logs/market_data/*.jsonl (historical)  /  live feed_store ticks
            │
            ▼
   Strategy interface: generate_signal(snapshot_history) -> Signal | None
      ├─ DirectionalStrategy      (SMA-score + spot momentum → CE/PE)
      └─ OptionsDynamicsStrategy  (PCR shift + OI-chg + IV move → CE/PE)
            │
            ▼
   QuantEngine (shared, one instance per strategy)
      - position sizing (delegates to capital_service math)
      - SL / target / trailing exit (single implementation, shared)
      - is_live() check: paper -> simulate fill at LTP (shadow_book-style)
                          live  -> order_service.schedule_*
            │
            ▼
   Book writer (shared code, per-strategy file):
      logs/quant_directional_trades.jsonl
      logs/quant_options_trades.jsonl
            │
            ▼
   Dashboard: Textual screen (tab per book) + web API routes per book
```

Backtest replays `logs/market_data/*.jsonl` through the exact same
`Strategy.generate_signal` and `QuantEngine` exit code used live (always in
paper mode), so a backtest number is a genuine prediction of live behavior —
not an approximation. This also fixes a known limitation in the current
`backtest/engine.py`, which approximates option premium via delta × spot move
because it predates recorded option-chain data; the new replay uses the real
`ce_ltp`/`pe_ltp`/`oi`/`iv`/`pcr` fields now available in `logs/market_data/`.

## Components

New module `src/uptox/quant/`:

- `strategy.py` — `Strategy` ABC: `generate_signal(history) -> Signal | None`.
  `Signal` dataclass: `option (CE|PE)`, `reason`, `score`, `strategy_id`.
- `directional_strategy.py` — rule-based, SMA-score + spot momentum thresholds,
  swept and validated against backtest expectancy.
- `options_dynamics_strategy.py` — rule-based, PCR shift + OI-chg + IV move
  thresholds, same validation approach.
- `engine.py` — `QuantEngine(strategy, book_path, mode_flag)`. Single
  implementation of sizing, SL, target, trailing exit, and the paper/live
  fork. Both strategies run through this — no duplicated exit logic.
- `quant_trading_mode.py` — same shape as existing `trading_mode.py`
  (`paper`/`live`, persisted, default `paper`), but **two independent
  instances**, one per strategy, separate from the existing global
  `trading_mode`. This is deliberate: `order_service` has no internal
  paper/live gate of its own (verified — it's caller-trusted, docstring-only
  convention), so `QuantEngine`'s own mode check is the *only* thing
  preventing a real order. Reusing the global flag would mean flipping
  focus-mode live silently arms an unproven quant model too.
- `book.py` — append-only JSONL open/close event writer, same shape as
  `shadow_book.py`, parameterized by path so both books share one
  implementation.

Backtest: `src/uptox/backtest/quant_replay.py` — replays historical snapshots
through `Strategy` + `QuantEngine` (paper mode, no exceptions), producing
per-strategy `BacktestResult`-style stats (trade count, win rate, avg
win/loss, expectancy, R:R, max drawdown, total P&L) for side-by-side
comparison.

Dashboard:
- `ui/screens/quant_book.py` — Textual screen, tab-switchable between the two
  books, mirrors the existing `trade_book.py` screen pattern.
- `web/app.py` — new routes `/api/quant/{directional,options}/trades` and
  `/api/quant/{directional,options}/stats`, following existing `web/data.py`
  / `schemas.py` conventions.

## Data flow

1. Live tick or historical snapshot arrives.
2. `Strategy.generate_signal(history)` returns a `Signal` or `None`.
3. If a `Signal` fires and `QuantEngine` has no open position for that
   strategy: size via `capital_service`, open paper (or live, if promoted)
   position, append `open` event to the strategy's book file.
4. On each subsequent tick: `QuantEngine` checks SL/target/trailing exit
   against the same option's live LTP; on exit, append `close` event with
   pnl_pts/pnl_rs/exit_reason.
5. Dashboard panels read the book files (or call the web API, which reads the
   same files) to render open position + trade history + running stats per
   strategy.
6. Backtest runs the identical steps 2-4 against historical
   `logs/market_data/*.jsonl` files in order, paper mode forced, producing a
   `BacktestResult` per strategy for comparison before any live promotion is
   considered.

## Error handling

- Book I/O failures (`OSError`) are caught and logged as a warning, never
  raised into the tick loop — matches `shadow_book.py`'s existing pattern.
- A `Strategy.generate_signal` exception is caught, logged with the
  strategy_id, and treated as "no signal this tick" — one bad tick must never
  crash the engine or the other strategy's independent instance.
- The paper/live fork inside `QuantEngine` is the single choke point for real
  order placement; it must not be duplicated or bypassed anywhere else in the
  quant module.

## Testing

- Unit tests per strategy: given a fixed synthetic snapshot history, assert
  the expected `Signal` (or `None`) — deterministic, no live dependencies.
- `QuantEngine` exit logic: unit tests for SL hit, target hit, trailing lock,
  session-close flatten — mirrors existing `test_mae_exit.py` /
  `test_trailing_sl.py` patterns.
- Explicit safety test: with `quant_trading_mode` set to `paper`, assert
  `order_service.schedule_order`/`schedule_entry_with_sl` are never called
  (mock and assert not-called) even when a `Signal` fires — this is the test
  for the one thing that must never silently break.
- Backtest replay: run against one known day of `logs/market_data/*.jsonl`,
  assert trade count and total P&L match a recorded expected value (snapshot
  test) so future changes to strategy/engine logic surface as an intentional
  diff, not silent drift.

## Open items for later (explicitly out of scope now)

- ML-based signal generation (v2, once more data accumulates).
- Actual promotion of either strategy to live order placement — a separate
  decision made after backtest + paper-forward results are reviewed.
