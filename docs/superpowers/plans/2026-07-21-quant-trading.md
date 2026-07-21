# Quant Trading Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build two independent, rule-based quant strategies (directional, options-dynamics) sharing one execution engine, backtest them on real recorded option-chain data, then run them live in isolated paper books (own logs, own dashboard panels) so they can be compared against focus mode before any live-promotion decision.

**Architecture:** A `Strategy` interface with two implementations feeds a shared `QuantEngine` that owns sizing, SL/target/trailing-exit, and the paper/live order fork. Each strategy gets its own `QuantEngine` instance, its own JSONL book file, and its own independent `QuantTradingMode` flag (default paper). Backtest replays `logs/market_data/*.jsonl` through the identical `Strategy` + `QuantEngine` code used live, so a backtest number is a real prediction of live behavior. Live wiring hooks into the existing 5-min bar-close point in `tick_handler.py` (same cadence `data_recorder.py` already uses), not raw ticks.

**Tech Stack:** Python 3.11+, stdlib only for the new module (`dataclasses`, `pathlib`, `json`, `datetime`), existing `order_service`/`capital_service` patterns mirrored (not reused directly — see Global Constraints), FastAPI for web routes, Textual for the TUI panel.

## Global Constraints

- No pytest installed in this venv (`uv run pytest` fails with "No module named pytest") — all tests in this plan are plain `assert`-based functions, run via a small inline runner (see Task 1 Step 2 for the exact invocation), matching the existing `tests/test_shadow_book.py` / `tests/test_mae_exit.py` style already in this repo. Do not add a pytest dependency as part of this plan.
- `order_service` has no internal paper/live gate of its own (verified by reading `src/uptox/services/order_service.py` — it's a docstring convention, not an enforced check). `QuantEngine`'s own `mode.is_live()` check is therefore the *only* thing preventing a real order — every task touching `QuantEngine`'s open/close path must keep that check intact and Task 7 must include the explicit safety test for it.
- `trailing_sl_service.py` and `capital_service.py`'s `_capital` are **module-level singletons** owned by the existing focus-mode system. Nothing in this plan may import and call into them for quant positions — a shared singleton would corrupt focus mode's real capital/trailing state. `QuantEngine` gets its own per-instance capital counter and its own exit-logic copy (mirroring the *pattern*, e.g. `_size_lots` from `src/uptox/backtest/engine.py:138-150`, not the module).
- Judge results by net expectancy after fees, never win rate alone (existing evaluation-standard decision, 2026-07-15).
- Both strategies are intraday-only — flat by session close `(15, 20)` IST, matching the existing system's convention (see `src/uptox/services/shadow_book.py:42`).
- No real order placement happens as a side effect of this plan. `QuantTradingMode` defaults to `"paper"` for both strategies; promotion to live is a manual, separate decision made after this plan ships.

---

## File Structure

```
src/uptox/quant/
    __init__.py
    strategy.py                  # Signal dataclass + Strategy ABC
    book.py                      # JSONL open/close event writer/reader + stats
    quant_trading_mode.py        # QuantTradingMode class, one instance per strategy
    directional_strategy.py      # DirectionalStrategy
    options_dynamics_strategy.py # OptionsDynamicsStrategy
    engine.py                    # QuantEngine — sizing, exits, paper/live fork
    runner.py                    # Wires 2 engines + rolling history, called from tick_handler

src/uptox/backtest/
    quant_replay.py              # Replays logs/market_data/*.jsonl through Strategy+QuantEngine

src/uptox/services/
    data_recorder.py             # MODIFY: record_bar() returns the row dict it wrote
    tick_handler.py              # MODIFY: bar-close block feeds row into quant.runner

src/uptox/web/
    quant_data.py                # Read helpers: book -> API-ready dicts
    app.py                       # MODIFY: add /api/quant/{strategy}/trades, /stats routes

src/uptox/ui/screens/
    quant_book.py                # Textual screen, tab per strategy book
src/uptox/ui/app.py              # MODIFY: register QuantBookScreen

tests/
    test_quant_strategy.py
    test_quant_book.py
    test_quant_trading_mode.py
    test_data_recorder.py
    test_quant_directional_strategy.py
    test_quant_options_dynamics_strategy.py
    test_quant_engine.py
    test_quant_runner.py
    test_quant_replay.py
    test_quant_web_data.py
```

Test-runner note used throughout (no pytest in this venv): every "Run test" step uses

```bash
uv run python -c "
import sys; sys.path.insert(0, 'tests'); sys.path.insert(0, 'src')
import MODULE_NAME as t
import traceback
fails = 0
for name in dir(t):
    if name.startswith('test_'):
        try:
            getattr(t, name)()
            print('PASS', name)
        except Exception as e:
            fails += 1
            print('FAIL', name, '->', e)
            traceback.print_exc()
print('failures:', fails)
"
```

with `MODULE_NAME` set to the test file's module name (no `.py`).

---

### Task 1: Signal + Strategy interface

**Files:**
- Create: `src/uptox/quant/__init__.py` (empty)
- Create: `src/uptox/quant/strategy.py`
- Test: `tests/test_quant_strategy.py`

**Interfaces:**
- Produces: `Signal` dataclass (`option: Literal["CE","PE"]`, `reason: str`, `score: float`, `strategy_id: str`); `Strategy` ABC with abstract method `generate_signal(self, history: list[dict]) -> Signal | None`. `history` is a list of market-snapshot dicts, newest last, same shape as one row of `logs/market_data/*.jsonl` (`nifty`, `bank`, `atm`, `ce_ltp`, `pe_ltp`, `ce_oi`, `pe_oi`, `ce_oi_chg`, `pe_oi_chg`, `ce_iv`, `pe_iv`, `pcr`, `cum_delta`, `sma20`, `sma50`, `sma_score`).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_quant_strategy.py
"""Signal dataclass shape and Strategy ABC contract."""
from uptox.quant.strategy import Signal, Strategy


def test_signal_fields():
    s = Signal(option="CE", reason="test", score=1.0, strategy_id="x")
    assert s.option == "CE"
    assert s.reason == "test"
    assert s.score == 1.0
    assert s.strategy_id == "x"


def test_strategy_is_abstract():
    try:
        Strategy()
        assert False, "Strategy() should not be instantiable"
    except TypeError:
        pass


def test_concrete_strategy_must_implement_generate_signal():
    class Dummy(Strategy):
        strategy_id = "dummy"

        def generate_signal(self, history):
            return Signal(option="PE", reason="always", score=0.0, strategy_id=self.strategy_id)

    d = Dummy()
    sig = d.generate_signal([])
    assert sig.option == "PE"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run python -c "
import sys; sys.path.insert(0, 'tests'); sys.path.insert(0, 'src')
import test_quant_strategy as t
t.test_signal_fields()
"
```
Expected: `ModuleNotFoundError: No module named 'uptox.quant'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/uptox/quant/__init__.py
```

```python
# src/uptox/quant/strategy.py
"""Strategy interface shared by every quant model. A Strategy consumes a
rolling history of market-snapshot dicts (same shape as one row of
logs/market_data/*.jsonl) and emits a Signal or nothing — it never touches
sizing, exits, or order placement (that's QuantEngine's job)."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Literal


@dataclass
class Signal:
    option: Literal["CE", "PE"]
    reason: str
    score: float
    strategy_id: str


class Strategy(ABC):
    strategy_id: str

    @abstractmethod
    def generate_signal(self, history: list[dict]) -> Signal | None:
        ...
```

- [ ] **Step 4: Run test to verify it passes**

```bash
uv run python -c "
import sys; sys.path.insert(0, 'tests'); sys.path.insert(0, 'src')
import test_quant_strategy as t
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
Expected: `PASS test_signal_fields`, `PASS test_strategy_is_abstract`, `PASS test_concrete_strategy_must_implement_generate_signal`, `failures: 0`

- [ ] **Step 5: Commit**

```bash
git add src/uptox/quant/__init__.py src/uptox/quant/strategy.py tests/test_quant_strategy.py
git commit -m "feat(quant): add Signal + Strategy interface"
```

---

### Task 2: Book — JSONL event writer/reader + stats

**Files:**
- Create: `src/uptox/quant/book.py`
- Test: `tests/test_quant_book.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `append_event(path: Path, row: dict) -> None`; `read_events(path: Path) -> list[dict]`; `closed_trades(path: Path) -> list[dict]` (pairs `open`/`close` events by `id` into flat trade dicts); `stats(trades: list[dict]) -> dict` (keys: `total`, `wins`, `losses`, `win_rate`, `total_pnl_pts`, `total_pnl_rs`, `expectancy_pts`, `expectancy_rs`).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_quant_book.py
"""JSONL open/close event pairing and stats — same shape convention as
shadow_book.py, parameterized by path so multiple books share this code."""
import json
import tempfile
from pathlib import Path

from uptox.quant import book


def _tmp_path() -> Path:
    return Path(tempfile.mkstemp(suffix=".jsonl")[1])


def test_append_and_read_events():
    p = _tmp_path()
    book.append_event(p, {"event": "open", "id": "a1", "option": "CE"})
    book.append_event(p, {"event": "close", "id": "a1", "pnl_pts": 5.0})
    rows = book.read_events(p)
    assert len(rows) == 2
    assert rows[0]["event"] == "open"
    assert rows[1]["event"] == "close"


def test_read_events_missing_file_returns_empty():
    assert book.read_events(Path("logs/does_not_exist_xyz.jsonl")) == []


def test_read_events_skips_corrupt_lines():
    p = _tmp_path()
    p.write_text('{"event": "open", "id": "a1"}\nNOT JSON\n{"event": "close", "id": "a1"}\n',
                 encoding="utf-8")
    rows = book.read_events(p)
    assert len(rows) == 2


def test_closed_trades_pairs_open_and_close():
    p = _tmp_path()
    book.append_event(p, {"event": "open", "id": "a1", "option": "CE", "entry_price": 100.0})
    book.append_event(p, {"event": "close", "id": "a1", "exit_price": 110.0,
                           "pnl_pts": 10.0, "pnl_rs": 6500.0})
    book.append_event(p, {"event": "open", "id": "a2", "option": "PE", "entry_price": 50.0})
    trades = book.closed_trades(p)
    assert len(trades) == 1  # a2 never closed
    assert trades[0]["option"] == "CE"
    assert trades[0]["exit_price"] == 110.0
    assert trades[0]["pnl_pts"] == 10.0


def test_stats_on_empty_trades():
    s = book.stats([])
    assert s["total"] == 0
    assert s["win_rate"] == 0.0
    assert s["expectancy_rs"] == 0.0


def test_stats_computes_win_rate_and_expectancy():
    trades = [
        {"pnl_pts": 10.0, "pnl_rs": 6500.0},
        {"pnl_pts": -5.0, "pnl_rs": -3250.0},
        {"pnl_pts": 8.0,  "pnl_rs": 5200.0},
    ]
    s = book.stats(trades)
    assert s["total"] == 3
    assert s["wins"] == 2
    assert s["losses"] == 1
    assert round(s["win_rate"], 1) == 66.7
    assert s["total_pnl_pts"] == 13.0
    assert s["total_pnl_rs"] == 8450.0
    assert round(s["expectancy_pts"], 2) == round(13.0 / 3, 2)
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run python -c "
import sys; sys.path.insert(0, 'tests'); sys.path.insert(0, 'src')
import test_quant_book as t
t.test_append_and_read_events()
"
```
Expected: `ModuleNotFoundError: No module named 'uptox.quant.book'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/uptox/quant/book.py
"""Append-only JSONL open/close event log, shared by every quant book —
same shape convention as shadow_book.py, parameterized by path so the
directional and options-dynamics books use identical code."""
from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)


def append_event(path: Path, row: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError as e:
        log.warning("quant.book: could not write %s: %s", path, e)


def read_events(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def closed_trades(path: Path) -> list[dict]:
    """Pair open/close events by id into flat trade dicts. An open without
    a matching close (still-open position, or app restarted mid-trade) is
    dropped — callers wanting the open position use a different lookup."""
    opens: dict[str, dict] = {}
    trades: list[dict] = []
    for row in read_events(path):
        rid = row.get("id")
        if row.get("event") == "open" and rid:
            opens[rid] = row
        elif row.get("event") == "close" and rid:
            o = opens.pop(rid, None)
            if o:
                trades.append({**o, **row})
    return trades


def stats(trades: list[dict]) -> dict:
    total = len(trades)
    if total == 0:
        return {
            "total": 0, "wins": 0, "losses": 0, "win_rate": 0.0,
            "total_pnl_pts": 0.0, "total_pnl_rs": 0.0,
            "expectancy_pts": 0.0, "expectancy_rs": 0.0,
        }
    wins   = sum(1 for t in trades if t.get("pnl_pts", 0.0) > 0)
    losses = total - wins
    total_pts = sum(t.get("pnl_pts", 0.0) for t in trades)
    total_rs  = sum(t.get("pnl_rs", 0.0) for t in trades)
    return {
        "total": total, "wins": wins, "losses": losses,
        "win_rate": round(wins / total * 100, 2),
        "total_pnl_pts": round(total_pts, 2),
        "total_pnl_rs": round(total_rs, 2),
        "expectancy_pts": round(total_pts / total, 2),
        "expectancy_rs": round(total_rs / total, 2),
    }
```

- [ ] **Step 4: Run test to verify it passes**

```bash
uv run python -c "
import sys; sys.path.insert(0, 'tests'); sys.path.insert(0, 'src')
import test_quant_book as t
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
Expected: 6 `PASS` lines, `failures: 0`

- [ ] **Step 5: Commit**

```bash
git add src/uptox/quant/book.py tests/test_quant_book.py
git commit -m "feat(quant): add shared JSONL book reader/writer + stats"
```

---

### Task 3: QuantTradingMode — independent per-strategy paper/live flag

**Files:**
- Create: `src/uptox/quant/quant_trading_mode.py`
- Test: `tests/test_quant_trading_mode.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `QuantTradingMode(strategy_id: str)` class with `.get_mode() -> str`, `.is_live() -> bool`, `.is_paper() -> bool`, `.set_mode(mode: str) -> None` (raises `ValueError` on invalid mode), persisted to `logs/quant_mode_{strategy_id}.json`, default `"paper"`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_quant_trading_mode.py
"""Two strategies must have fully independent paper/live flags — flipping
one must never affect the other or the global trading_mode."""
import json
import tempfile
from pathlib import Path

from uptox.quant.quant_trading_mode import QuantTradingMode


def test_defaults_to_paper():
    m = QuantTradingMode("test_a")
    m._file = Path(tempfile.mkstemp(suffix=".json")[1])
    m._file.unlink()  # no prior state
    m._mode = "paper"
    assert m.is_paper()
    assert not m.is_live()


def test_set_mode_persists_and_loads():
    tmp = Path(tempfile.mkstemp(suffix=".json")[1])
    m = QuantTradingMode("test_b")
    m._file = tmp
    m.set_mode("live")
    assert m.is_live()

    m2 = QuantTradingMode("test_b")
    m2._file = tmp
    m2._load()
    assert m2.is_live()


def test_invalid_mode_raises():
    m = QuantTradingMode("test_c")
    try:
        m.set_mode("bogus")
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_two_instances_are_independent():
    a = QuantTradingMode("test_d1")
    a._file = Path(tempfile.mkstemp(suffix=".json")[1])
    b = QuantTradingMode("test_d2")
    b._file = Path(tempfile.mkstemp(suffix=".json")[1])
    a.set_mode("live")
    assert a.is_live()
    assert b.is_paper()  # unaffected by a
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run python -c "
import sys; sys.path.insert(0, 'tests'); sys.path.insert(0, 'src')
import test_quant_trading_mode as t
t.test_defaults_to_paper()
"
```
Expected: `ModuleNotFoundError: No module named 'uptox.quant.quant_trading_mode'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/uptox/quant/quant_trading_mode.py
"""Paper/live flag for one quant strategy — independent of the global
trading_mode and of every other strategy's flag. order_service has no
internal paper/live gate of its own (verified against order_service.py),
so this check is the only thing standing between a quant signal and a
real order. Reusing the global flag would mean flipping focus-mode live
silently arms an unproven quant model too — hence one instance per
strategy_id, not a module singleton."""
from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)

_VALID = ("paper", "live")


class QuantTradingMode:

    def __init__(self, strategy_id: str):
        self.strategy_id = strategy_id
        self._file = Path(f"logs/quant_mode_{strategy_id}.json")
        self._mode = "paper"
        self._load()

    def get_mode(self) -> str:
        return self._mode

    def is_live(self) -> bool:
        return self._mode == "live"

    def is_paper(self) -> bool:
        return self._mode == "paper"

    def set_mode(self, mode: str) -> None:
        if mode not in _VALID:
            raise ValueError(f"Invalid mode '{mode}'. Must be one of {_VALID}")
        self._mode = mode
        self._save()
        log.info("Quant mode [%s] -> %s", self.strategy_id, mode.upper())

    def _save(self) -> None:
        try:
            self._file.parent.mkdir(parents=True, exist_ok=True)
            self._file.write_text(json.dumps({"mode": self._mode}), encoding="utf-8")
        except OSError as e:
            log.error("quant_trading_mode save error [%s]: %s", self.strategy_id, e)

    def _load(self) -> None:
        if self._file.exists():
            try:
                data = json.loads(self._file.read_text(encoding="utf-8"))
                m = data.get("mode", "paper")
                if m in _VALID:
                    self._mode = m
            except (OSError, json.JSONDecodeError) as e:
                log.error("quant_trading_mode load error [%s]: %s", self.strategy_id, e)
```

- [ ] **Step 4: Run test to verify it passes**

```bash
uv run python -c "
import sys; sys.path.insert(0, 'tests'); sys.path.insert(0, 'src')
import test_quant_trading_mode as t
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
Expected: 4 `PASS` lines, `failures: 0`

- [ ] **Step 5: Commit**

```bash
git add src/uptox/quant/quant_trading_mode.py tests/test_quant_trading_mode.py
git commit -m "feat(quant): add independent per-strategy paper/live mode flag"
```

---

### Task 4: data_recorder.record_bar() returns the snapshot row

**Files:**
- Modify: `src/uptox/services/data_recorder.py:41-121`
- Test: `tests/test_data_recorder.py`

**Interfaces:**
- Consumes: nothing from earlier tasks (independent of `quant/`).
- Produces: `record_bar() -> dict | None` — same `row` dict already written to `logs/market_data/*.jsonl`, or `None` if the bar was skipped (no `nifty_ltp`) or an exception occurred. Task 8's `runner.py` and Task 10's `tick_handler.py` change consume this return value.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_data_recorder.py
"""record_bar() must return the row it wrote (or None on skip/error) so
live callers — the quant runner — can consume the same snapshot without
re-deriving it or re-reading the file it was just written to."""
import tempfile
from pathlib import Path
from types import SimpleNamespace

from uptox.services import data_recorder, feed_store, option_chain_service, sma_service
from uptox.services.sma_service import TimeFrame


def _wire(nifty_ltp: float, bank_ltp: float = 55000.0):
    data_recorder._DATA_DIR = Path(tempfile.mkdtemp())
    feed_store.get = lambda key: (
        SimpleNamespace(ltp=nifty_ltp) if "Nifty 50" in key else
        SimpleNamespace(ltp=bank_ltp) if "Nifty Bank" in key else
        SimpleNamespace(ltp=100.0, oi=1000, change_oi=10, iv=15.0)
    )
    option_chain_service.get_option = lambda strike, opt: {
        "ltp": 100.0, "oi": 1000, "change_oi": 10, "iv": 15.0,
    }
    option_chain_service.get_key = lambda strike, opt: f"NSE_FO|{strike}{opt}"
    feed_store.cum_delta = lambda key: 0.0
    sma_service.get_smas = lambda key, tf: {20: nifty_ltp - 10, 50: nifty_ltp - 20}


def test_record_bar_returns_row_shape():
    _wire(24200.0)
    row = data_recorder.record_bar()
    assert row is not None
    for field in ("ts", "nifty", "bank", "atm", "ce_ltp", "pe_ltp", "ce_oi", "pe_oi",
                  "ce_oi_chg", "pe_oi_chg", "ce_iv", "pe_iv", "pcr", "cum_delta",
                  "sma20", "sma50", "sma_score"):
        assert field in row, f"missing field {field}"
    assert row["nifty"] == 24200.0
    assert row["atm"] == 24200


def test_record_bar_returns_none_without_price():
    data_recorder._DATA_DIR = Path(tempfile.mkdtemp())
    feed_store.get = lambda key: None
    assert data_recorder.record_bar() is None
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run python -c "
import sys; sys.path.insert(0, 'tests'); sys.path.insert(0, 'src')
import test_data_recorder as t
t.test_record_bar_returns_row_shape()
"
```
Expected: `AssertionError: assert None is not None` (current `record_bar()` returns `None` implicitly)

- [ ] **Step 3: Write minimal implementation**

Modify `src/uptox/services/data_recorder.py`. Change the signature on line 41 and add a `return row` at the very end of the `try` block (after the file write), and `return None` in the existing early-return and except paths:

```python
def record_bar() -> dict | None:
    """
    Snapshot current market state and append one row to today's JSONL file.
    Called once per 5-min bar close from tick_handler. Returns the row it
    wrote so live callers (the quant runner) can consume the identical
    snapshot instead of re-deriving it — or None if the bar was skipped
    (no price yet) or an error occurred.
    """
    try:
        from uptox.services import feed_store, option_chain_service, sma_service
        from uptox.services.sma_service import TimeFrame

        now   = datetime.now(_IST)
        nifty = feed_store.get(_NIFTY)
        bank  = feed_store.get(_BANK)

        nifty_ltp = (nifty.ltp if nifty else 0.0) or 0.0
        bank_ltp  = (bank.ltp  if bank  else 0.0) or 0.0

        if not nifty_ltp:
            return None  # no price — skip this bar

        atm = round(nifty_ltp / 50) * 50

        ce_sn = option_chain_service.get_option(atm, "CE") or {}
        pe_sn = option_chain_service.get_option(atm, "PE") or {}

        ce_key = option_chain_service.get_key(atm, "CE")
        pe_key = option_chain_service.get_key(atm, "PE")
        ce_t   = feed_store.get(ce_key) if ce_key else None
        pe_t   = feed_store.get(pe_key) if pe_key else None

        def _lv(tick, snap, field):
            live = getattr(tick, field, None) if tick else None
            return live if live else snap.get(field)

        ce_ltp    = _lv(ce_t, ce_sn, "ltp")    or 0.0
        pe_ltp    = _lv(pe_t, pe_sn, "ltp")    or 0.0
        ce_oi     = _lv(ce_t, ce_sn, "oi")     or 0.0
        pe_oi     = _lv(pe_t, pe_sn, "oi")     or 0.0
        ce_oi_chg = _lv(ce_t, ce_sn, "change_oi") or 0.0
        pe_oi_chg = _lv(pe_t, pe_sn, "change_oi") or 0.0
        ce_iv     = _lv(ce_t, ce_sn, "iv")     or 0.0
        pe_iv     = _lv(pe_t, pe_sn, "iv")     or 0.0

        pcr = round(pe_oi / ce_oi, 3) if ce_oi else 0.0

        cum_delta = feed_store.cum_delta(ce_key) if ce_key else 0.0

        smas  = sma_service.get_smas(_NIFTY, TimeFrame.M5)
        s20   = smas.get(20) or 0.0
        s50   = smas.get(50) or 0.0
        sc20  = (1 if nifty_ltp > s20 else -1) if s20 else 0
        sc50  = (1 if nifty_ltp > s50 else -1) if s50 else 0
        sma_score = sc20 + sc50

        row = {
            "ts":        now.isoformat(),
            "nifty":     round(nifty_ltp, 2),
            "bank":      round(bank_ltp, 2),
            "atm":       atm,
            "ce_ltp":    round(ce_ltp, 2),
            "pe_ltp":    round(pe_ltp, 2),
            "ce_oi":     int(ce_oi),
            "pe_oi":     int(pe_oi),
            "ce_oi_chg": int(ce_oi_chg),
            "pe_oi_chg": int(pe_oi_chg),
            "ce_iv":     round(ce_iv, 2),
            "pe_iv":     round(pe_iv, 2),
            "pcr":       pcr,
            "cum_delta": round(cum_delta, 2),
            "sma20":     round(s20, 2),
            "sma50":     round(s50, 2),
            "sma_score": sma_score,
        }

        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        fname = _DATA_DIR / f"{now.strftime('%Y-%m-%d')}.jsonl"
        with fname.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

        return row

    except Exception as e:
        log.error("data_recorder.record_bar error: %s", e)
        return None
```

- [ ] **Step 4: Run test to verify it passes**

```bash
uv run python -c "
import sys; sys.path.insert(0, 'tests'); sys.path.insert(0, 'src')
import test_data_recorder as t
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
Expected: 2 `PASS` lines, `failures: 0`

- [ ] **Step 5: Commit**

```bash
git add src/uptox/services/data_recorder.py tests/test_data_recorder.py
git commit -m "feat(data_recorder): return the snapshot row record_bar writes"
```

---

### Task 5: DirectionalStrategy

**Files:**
- Create: `src/uptox/quant/directional_strategy.py`
- Test: `tests/test_quant_directional_strategy.py`

**Interfaces:**
- Consumes: `Signal`, `Strategy` from Task 1 (`src/uptox/quant/strategy.py`).
- Produces: `DirectionalStrategy` class, `strategy_id = "directional"`, `generate_signal(history: list[dict]) -> Signal | None`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_quant_directional_strategy.py
"""Directional signal: SMA-score at the extreme (+2/-2) confirmed by the
last bar's spot momentum in the same direction. Needs >=2 history rows."""
from uptox.quant.directional_strategy import DirectionalStrategy


def _row(nifty, sma_score):
    return {"nifty": nifty, "sma_score": sma_score}


def test_no_signal_with_less_than_2_rows():
    s = DirectionalStrategy()
    assert s.generate_signal([]) is None
    assert s.generate_signal([_row(24200, 2)]) is None


def test_ce_signal_on_score_plus2_and_uptick():
    s = DirectionalStrategy()
    history = [_row(24190, 2), _row(24200, 2)]  # moved up, score maxed bullish
    sig = s.generate_signal(history)
    assert sig is not None
    assert sig.option == "CE"
    assert sig.strategy_id == "directional"


def test_pe_signal_on_score_minus2_and_downtick():
    s = DirectionalStrategy()
    history = [_row(24210, -2), _row(24200, -2)]  # moved down, score maxed bearish
    sig = s.generate_signal(history)
    assert sig is not None
    assert sig.option == "PE"


def test_no_signal_when_score_not_extreme():
    s = DirectionalStrategy()
    history = [_row(24190, 1), _row(24200, 1)]
    assert s.generate_signal(history) is None


def test_no_signal_when_score_extreme_but_price_flat():
    s = DirectionalStrategy()
    history = [_row(24200, 2), _row(24200, 2)]  # no momentum confirmation
    assert s.generate_signal(history) is None
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run python -c "
import sys; sys.path.insert(0, 'tests'); sys.path.insert(0, 'src')
import test_quant_directional_strategy as t
t.test_ce_signal_on_score_plus2_and_uptick()
"
```
Expected: `ModuleNotFoundError: No module named 'uptox.quant.directional_strategy'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/uptox/quant/directional_strategy.py
"""Predicts NIFTY spot direction: SMA-score at its extreme (+2 or -2)
confirmed by the last bar's momentum in the same direction, expressed by
buying ATM CE/PE. v1 thresholds are deliberately simple (score extreme +
1-bar momentum) — sweep and validate against backtest expectancy before
tightening; ML is out of scope until more data accumulates."""
from __future__ import annotations

from uptox.quant.strategy import Signal, Strategy


class DirectionalStrategy(Strategy):
    strategy_id = "directional"

    def generate_signal(self, history: list[dict]) -> Signal | None:
        if len(history) < 2:
            return None
        cur, prev = history[-1], history[-2]
        score = cur.get("sma_score", 0)
        moved_up   = cur.get("nifty", 0) > prev.get("nifty", 0)
        moved_down = cur.get("nifty", 0) < prev.get("nifty", 0)

        if score == 2 and moved_up:
            return Signal(option="CE", reason="sma_score+2_momentum_up",
                          score=float(score), strategy_id=self.strategy_id)
        if score == -2 and moved_down:
            return Signal(option="PE", reason="sma_score-2_momentum_down",
                          score=float(score), strategy_id=self.strategy_id)
        return None
```

- [ ] **Step 4: Run test to verify it passes**

```bash
uv run python -c "
import sys; sys.path.insert(0, 'tests'); sys.path.insert(0, 'src')
import test_quant_directional_strategy as t
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
Expected: 5 `PASS` lines, `failures: 0`

- [ ] **Step 5: Commit**

```bash
git add src/uptox/quant/directional_strategy.py tests/test_quant_directional_strategy.py
git commit -m "feat(quant): add DirectionalStrategy (SMA-score + momentum)"
```

---

### Task 6: OptionsDynamicsStrategy

**Files:**
- Create: `src/uptox/quant/options_dynamics_strategy.py`
- Test: `tests/test_quant_options_dynamics_strategy.py`

**Interfaces:**
- Consumes: `Signal`, `Strategy` from Task 1.
- Produces: `OptionsDynamicsStrategy` class, `strategy_id = "options_dynamics"`, `generate_signal(history: list[dict]) -> Signal | None`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_quant_options_dynamics_strategy.py
"""Options-dynamics signal: OI buildup direction (using this codebase's
existing convention from option_chain_service.get_oi_signal — CE-OI-up +
PE-OI-down reads bullish) confirmed by PCR level and matching IV move."""
from uptox.quant.options_dynamics_strategy import OptionsDynamicsStrategy


def _row(ce_oi_chg, pe_oi_chg, pcr, ce_iv, pe_iv):
    return {"ce_oi_chg": ce_oi_chg, "pe_oi_chg": pe_oi_chg, "pcr": pcr,
            "ce_iv": ce_iv, "pe_iv": pe_iv}


def test_no_signal_on_empty_history():
    s = OptionsDynamicsStrategy()
    assert s.generate_signal([]) is None


def test_ce_signal_on_bullish_oi_low_pcr():
    s = OptionsDynamicsStrategy()
    history = [_row(ce_oi_chg=5000, pe_oi_chg=-3000, pcr=0.8, ce_iv=14.0, pe_iv=12.0)]
    sig = s.generate_signal(history)
    assert sig is not None
    assert sig.option == "CE"
    assert sig.strategy_id == "options_dynamics"


def test_pe_signal_on_bearish_oi_high_pcr():
    s = OptionsDynamicsStrategy()
    history = [_row(ce_oi_chg=-4000, pe_oi_chg=6000, pcr=1.4, ce_iv=11.0, pe_iv=13.0)]
    sig = s.generate_signal(history)
    assert sig is not None
    assert sig.option == "PE"


def test_no_signal_when_oi_direction_conflicts_with_pcr():
    s = OptionsDynamicsStrategy()
    # bullish OI pattern but PCR still high (>1.0) — not confirmed
    history = [_row(ce_oi_chg=5000, pe_oi_chg=-3000, pcr=1.3, ce_iv=14.0, pe_iv=12.0)]
    assert s.generate_signal(history) is None


def test_no_signal_when_oi_moves_same_direction():
    s = OptionsDynamicsStrategy()
    history = [_row(ce_oi_chg=3000, pe_oi_chg=3000, pcr=1.0, ce_iv=12.0, pe_iv=12.0)]
    assert s.generate_signal(history) is None
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run python -c "
import sys; sys.path.insert(0, 'tests'); sys.path.insert(0, 'src')
import test_quant_options_dynamics_strategy as t
t.test_ce_signal_on_bullish_oi_low_pcr()
"
```
Expected: `ModuleNotFoundError: No module named 'uptox.quant.options_dynamics_strategy'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/uptox/quant/options_dynamics_strategy.py
"""Trades the option's own behavior — OI buildup, PCR level, IV move —
independent of a pure directional call. OI-direction reading reuses this
codebase's existing convention (option_chain_service.get_oi_signal):
CE-OI-up + PE-OI-down = bullish, and the reverse = bearish. v1 thresholds
are deliberately simple; sweep and validate against backtest expectancy
before tightening."""
from __future__ import annotations

from uptox.quant.strategy import Signal, Strategy


class OptionsDynamicsStrategy(Strategy):
    strategy_id = "options_dynamics"

    def generate_signal(self, history: list[dict]) -> Signal | None:
        if not history:
            return None
        cur = history[-1]
        ce_chg = cur.get("ce_oi_chg", 0)
        pe_chg = cur.get("pe_oi_chg", 0)
        ce_iv  = cur.get("ce_iv", 0.0)
        pe_iv  = cur.get("pe_iv", 0.0)
        pcr    = cur.get("pcr", 1.0)

        if ce_chg > 0 and pe_chg < 0 and pcr < 1.0 and ce_iv >= pe_iv:
            return Signal(option="CE", reason="oi_bullish_pcr_low",
                          score=1.0, strategy_id=self.strategy_id)
        if ce_chg < 0 and pe_chg > 0 and pcr > 1.0 and pe_iv >= ce_iv:
            return Signal(option="PE", reason="oi_bearish_pcr_high",
                          score=-1.0, strategy_id=self.strategy_id)
        return None
```

- [ ] **Step 4: Run test to verify it passes**

```bash
uv run python -c "
import sys; sys.path.insert(0, 'tests'); sys.path.insert(0, 'src')
import test_quant_options_dynamics_strategy as t
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
Expected: 5 `PASS` lines, `failures: 0`

- [ ] **Step 5: Commit**

```bash
git add src/uptox/quant/options_dynamics_strategy.py tests/test_quant_options_dynamics_strategy.py
git commit -m "feat(quant): add OptionsDynamicsStrategy (OI + PCR + IV)"
```

---

### Task 7: QuantEngine — sizing, exits, paper/live fork

**Files:**
- Create: `src/uptox/quant/engine.py`
- Test: `tests/test_quant_engine.py`

**Interfaces:**
- Consumes: `Signal`, `Strategy` (Task 1); `book.append_event` (Task 2); `QuantTradingMode` (Task 3).
- Produces: `QuantEngine(strategy: Strategy, book_path: Path, mode: QuantTradingMode, capital: float = 20_000.0)` with `.on_snapshot(snapshot: dict, history: list[dict]) -> None` and `.position: dict | None` attribute. Task 8 (`runner.py`) instantiates two of these; Task 9 (`quant_replay.py`) drives them from historical data.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_quant_engine.py
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
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run python -c "
import sys; sys.path.insert(0, 'tests'); sys.path.insert(0, 'src')
import test_quant_engine as t
t.test_opens_position_on_signal()
"
```
Expected: `ModuleNotFoundError: No module named 'uptox.quant.engine'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/uptox/quant/engine.py
"""Shared execution engine for every quant strategy — sizing, SL/target/
trailing exit, and the paper/live order fork all live here once, not
duplicated per strategy. This is a fresh, self-contained implementation:
it does NOT call trailing_sl_service or capital_service's module-level
singletons, which belong to the existing focus-mode system — reusing them
would corrupt focus mode's real trailing/capital state (see Global
Constraints). Sizing math mirrors backtest/engine.py's _size_lots, same
convention as that file's own "mirrors capital_service logic" comment.
Exit model mirrors shadow_book.py's proven simple pattern (hard stop +
profit lock), not reused directly for the same singleton-state reason."""
from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from uptox.quant import book
from uptox.quant.quant_trading_mode import QuantTradingMode
from uptox.quant.strategy import Signal, Strategy

log = logging.getLogger(__name__)
_IST = ZoneInfo("Asia/Kolkata")

LOT_SIZE      = 65
STOP_PTS      = 10.0
LOCK_ARM_PTS  = 6.0
LOCK_KEEP     = 0.50
LOCK_BIG_PTS  = 15.0
LOCK_BIG_KEEP = 0.60
RR            = 2.0
SESSION_CLOSE = (15, 20)


def _size_lots(capital: float, entry: float, sl: float, lot_size: int = LOT_SIZE) -> int:
    risk_per_lot = (entry - sl) * lot_size
    if risk_per_lot <= 0 or capital <= 0:
        return 0
    affordable = max(1, int(capital / (entry * lot_size)))
    if capital < 25_000:
        return min(1, affordable)
    elif capital < 1_00_000:
        max_risk = capital * 0.05
        return min(affordable, max(1, int(max_risk / risk_per_lot)))
    else:
        max_risk = capital * 0.02
        return min(affordable, max(1, int(max_risk / risk_per_lot)), 10)


class QuantEngine:

    def __init__(self, strategy: Strategy, book_path: Path, mode: QuantTradingMode,
                 capital: float = 20_000.0):
        self.strategy  = strategy
        self.book_path = book_path
        self.mode      = mode
        self.capital   = capital
        self.position: dict | None = None

    def on_snapshot(self, snapshot: dict, history: list[dict]) -> None:
        if self.position is None:
            try:
                signal = self.strategy.generate_signal(history)
            except Exception as e:
                log.warning("[quant:%s] generate_signal raised, treating as no signal: %s",
                            self.strategy.strategy_id, e)
                signal = None
            if signal:
                self._open(signal, snapshot)
        else:
            self._check_exit(snapshot)

    def _open(self, signal: Signal, snapshot: dict) -> None:
        entry = snapshot.get("ce_ltp") if signal.option == "CE" else snapshot.get("pe_ltp")
        if not entry or entry <= 0:
            return
        sl     = round(entry - STOP_PTS, 1)
        target = round(entry + STOP_PTS * RR, 1)
        lots   = _size_lots(self.capital, entry, sl)
        if lots <= 0:
            return

        now = datetime.now(_IST)
        pos_id = f"{signal.strategy_id}-{now.strftime('%H%M%S%f')}"
        self.position = {
            "id": pos_id, "date": now.strftime("%Y-%m-%d"),
            "strategy_id": signal.strategy_id, "option": signal.option,
            "entry_price": entry, "spot_entry": snapshot.get("nifty", 0.0),
            "reason": signal.reason, "lots": lots, "sl": sl, "target": target,
            "peak": entry,
        }
        book.append_event(self.book_path, {
            "event": "open", "ts": now.isoformat(), "id": pos_id,
            "date": self.position["date"], "strategy_id": signal.strategy_id,
            "option": signal.option, "entry_price": entry,
            "spot_entry": self.position["spot_entry"], "reason": signal.reason,
            "lots": lots, "sl": sl, "target": target,
        })
        log.info("[quant:%s] OPEN %s @ %.1f (%s)",
                  signal.strategy_id, signal.option, entry, signal.reason)

        if self.mode.is_live():
            from uptox.services import order_service, option_chain_service
            atm = round(self.position["spot_entry"] / 50) * 50
            key = option_chain_service.get_key(atm, signal.option)
            if key:
                order_service.schedule_entry_with_sl(key, lots * LOT_SIZE, entry, sl)

    def _check_exit(self, snapshot: dict) -> None:
        pos = self.position
        ltp = snapshot.get("ce_ltp") if pos["option"] == "CE" else snapshot.get("pe_ltp")
        if not ltp or ltp <= 0:
            return
        if ltp > pos["peak"]:
            pos["peak"] = ltp

        now = datetime.now(_IST)
        if (now.hour, now.minute) >= SESSION_CLOSE:
            self._close(ltp, "session_close")
            return
        if ltp >= pos["target"]:
            self._close(ltp, "target")
            return
        if ltp <= pos["sl"]:
            self._close(ltp, "stop")
            return
        peak_gain = pos["peak"] - pos["entry_price"]
        if peak_gain >= LOCK_ARM_PTS:
            keep  = LOCK_BIG_KEEP if peak_gain >= LOCK_BIG_PTS else LOCK_KEEP
            floor = pos["entry_price"] + peak_gain * keep
            if ltp <= floor:
                self._close(ltp, "profit_lock")

    def _close(self, exit_price: float, reason: str) -> None:
        pos, self.position = self.position, None
        pnl_pts = round(exit_price - pos["entry_price"], 1)
        pnl_rs  = round(pnl_pts * pos["lots"] * LOT_SIZE, 0)
        self.capital += pnl_rs
        book.append_event(self.book_path, {
            "event": "close", "ts": datetime.now(_IST).isoformat(), "id": pos["id"],
            "exit_price": round(exit_price, 1), "exit_reason": reason,
            "pnl_pts": pnl_pts, "pnl_rs": pnl_rs,
            "peak_pts": round(pos["peak"] - pos["entry_price"], 1),
        })
        log.info("[quant:%s] CLOSE %s @ %.1f (%s) pnl %+.1fpt",
                  pos["strategy_id"], pos["option"], exit_price, reason, pnl_pts)

        if self.mode.is_live():
            from uptox.services import order_service, option_chain_service
            atm = round(pos["spot_entry"] / 50) * 50
            key = option_chain_service.get_key(atm, pos["option"])
            if key:
                order_service.schedule_exit(key, pos["lots"] * LOT_SIZE, exit_price)
```

- [ ] **Step 4: Run test to verify it passes**

```bash
uv run python -c "
import sys; sys.path.insert(0, 'tests'); sys.path.insert(0, 'src')
import test_quant_engine as t
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
Expected: 9 `PASS` lines including `PASS test_paper_mode_never_calls_order_service` and `PASS test_strategy_exception_is_caught_and_treated_as_no_signal`, `failures: 0`

- [ ] **Step 5: Commit**

```bash
git add src/uptox/quant/engine.py tests/test_quant_engine.py
git commit -m "feat(quant): add QuantEngine — sizing, exits, paper/live fork"
```

---

### Task 8: quant/runner.py — wires both engines to the live bar-close feed

**Files:**
- Create: `src/uptox/quant/runner.py`
- Test: `tests/test_quant_runner.py`

**Interfaces:**
- Consumes: `DirectionalStrategy` (Task 5), `OptionsDynamicsStrategy` (Task 6), `QuantEngine`, `QuantTradingMode` (Tasks 7, 3).
- Produces: `QuantRunner()` class with `.on_bar(row: dict) -> None` (feeds `row` into a rolling history buffer and both engines) and `.engines: dict[str, QuantEngine]` (keys `"directional"`, `"options_dynamics"`). Task 10 wires this into `tick_handler.py`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_quant_runner.py
"""QuantRunner keeps one rolling history shared by both strategies and
forwards every bar-close snapshot to both engines."""
import tempfile
from pathlib import Path

from uptox.quant.runner import QuantRunner


def _row(nifty, sma_score, ce_ltp=150.0, pe_ltp=80.0, ce_oi_chg=0, pe_oi_chg=0, pcr=1.0,
         ce_iv=12.0, pe_iv=12.0):
    return {"nifty": nifty, "sma_score": sma_score, "ce_ltp": ce_ltp, "pe_ltp": pe_ltp,
            "ce_oi_chg": ce_oi_chg, "pe_oi_chg": pe_oi_chg, "pcr": pcr,
            "ce_iv": ce_iv, "pe_iv": pe_iv}


def _runner():
    r = QuantRunner()
    for strat_id, eng in r.engines.items():
        eng.book_path = Path(tempfile.mkstemp(suffix=".jsonl")[1])
        eng.mode._file = Path(tempfile.mkstemp(suffix=".json")[1])
        eng.mode._mode = "paper"
    return r


def test_has_both_engines():
    r = _runner()
    assert set(r.engines.keys()) == {"directional", "options_dynamics"}


def test_on_bar_appends_to_history():
    r = _runner()
    r.on_bar(_row(24200, 0))
    assert len(r.history) == 1


def test_on_bar_can_trigger_directional_engine():
    r = _runner()
    r.on_bar(_row(24190, 2))
    r.on_bar(_row(24200, 2))  # score+2, uptick -> directional CE signal
    assert r.engines["directional"].position is not None
    assert r.engines["directional"].position["option"] == "CE"


def test_on_bar_can_trigger_options_dynamics_engine():
    r = _runner()
    r.on_bar(_row(24200, 0, ce_oi_chg=5000, pe_oi_chg=-3000, pcr=0.8, ce_iv=14.0, pe_iv=12.0))
    assert r.engines["options_dynamics"].position is not None
    assert r.engines["options_dynamics"].position["option"] == "CE"


def test_ignores_none_row():
    r = _runner()
    r.on_bar(None)
    assert len(r.history) == 0
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run python -c "
import sys; sys.path.insert(0, 'tests'); sys.path.insert(0, 'src')
import test_quant_runner as t
t.test_has_both_engines()
"
```
Expected: `ModuleNotFoundError: No module named 'uptox.quant.runner'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/uptox/quant/runner.py
"""Wires both quant strategies to the live 5-min bar-close feed — same
snapshot shape and cadence data_recorder.py already writes to
logs/market_data/*.jsonl, so live and backtest run identical code (see
backtest/quant_replay.py). One rolling history buffer feeds both
strategies; each strategy gets its own QuantEngine instance, own book
file, own independent paper/live mode."""
from __future__ import annotations

from pathlib import Path

from uptox.quant.directional_strategy import DirectionalStrategy
from uptox.quant.engine import QuantEngine
from uptox.quant.options_dynamics_strategy import OptionsDynamicsStrategy
from uptox.quant.quant_trading_mode import QuantTradingMode

_HISTORY_MAX = 500  # ~1.5 trading days of 5-min bars — plenty for the v1 rule windows


class QuantRunner:

    def __init__(self) -> None:
        self.history: list[dict] = []
        directional = DirectionalStrategy()
        options_dyn = OptionsDynamicsStrategy()
        self.engines: dict[str, QuantEngine] = {
            "directional": QuantEngine(
                directional,
                Path("logs/quant_directional_trades.jsonl"),
                QuantTradingMode("directional"),
            ),
            "options_dynamics": QuantEngine(
                options_dyn,
                Path("logs/quant_options_trades.jsonl"),
                QuantTradingMode("options_dynamics"),
            ),
        }

    def on_bar(self, row: dict | None) -> None:
        if row is None:
            return
        self.history.append(row)
        if len(self.history) > _HISTORY_MAX:
            self.history = self.history[-_HISTORY_MAX:]
        for engine in self.engines.values():
            engine.on_snapshot(row, self.history)
```

- [ ] **Step 4: Run test to verify it passes**

```bash
uv run python -c "
import sys; sys.path.insert(0, 'tests'); sys.path.insert(0, 'src')
import test_quant_runner as t
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
Expected: 5 `PASS` lines, `failures: 0`

- [ ] **Step 5: Commit**

```bash
git add src/uptox/quant/runner.py tests/test_quant_runner.py
git commit -m "feat(quant): add QuantRunner wiring both engines to bar-close feed"
```

---

### Task 9: Hook QuantRunner into tick_handler.py's bar-close block

**Files:**
- Modify: `src/uptox/services/tick_handler.py:94-98`

**Interfaces:**
- Consumes: `data_recorder.record_bar() -> dict | None` (Task 4), `QuantRunner` (Task 8).
- Produces: live quant signal generation actually runs. No new test file — this is a two-line wiring change; verified by the existing Task 4 and Task 8 tests plus a manual smoke check in Step 3.

- [ ] **Step 1: Read the current bar-close block**

```bash
uv run python -c "
lines = open('src/uptox/services/tick_handler.py', encoding='utf-8').readlines()
print(''.join(lines[85:100]))
"
```
Expected output shows a block around line 96-97:
```python
            from uptox.services.data_recorder import record_bar
            record_bar()
```

- [ ] **Step 2: Modify the bar-close block**

Change:
```python
            from uptox.services.data_recorder import record_bar
            record_bar()
```
to:
```python
            from uptox.services.data_recorder import record_bar
            _bar_row = record_bar()
            from uptox.quant.runner import _get_runner
            _get_runner().on_bar(_bar_row)
```

Add a module-level singleton accessor at the bottom of `src/uptox/quant/runner.py` (append, do not replace the class):

```python
# --- appended to src/uptox/quant/runner.py ---
_runner: QuantRunner | None = None


def _get_runner() -> QuantRunner:
    global _runner
    if _runner is None:
        _runner = QuantRunner()
    return _runner
```

- [ ] **Step 3: Manual smoke check — import chain has no circular import or syntax error**

```bash
uv run python -c "
import sys; sys.path.insert(0, 'src')
from uptox.services import tick_handler
from uptox.quant.runner import _get_runner
r = _get_runner()
print('engines:', list(r.engines.keys()))
print('OK — no import errors')
"
```
Expected: `engines: ['directional', 'options_dynamics']` then `OK — no import errors`

- [ ] **Step 4: Re-run Task 4 and Task 8 test suites to confirm no regression**

```bash
uv run python -c "
import sys; sys.path.insert(0, 'tests'); sys.path.insert(0, 'src')
for mod_name in ('test_data_recorder', 'test_quant_runner'):
    mod = __import__(mod_name)
    for name in dir(mod):
        if name.startswith('test_'):
            try:
                getattr(mod, name)(); print('PASS', mod_name + '.' + name)
            except Exception as e:
                print('FAIL', mod_name + '.' + name, '->', e)
"
```
Expected: all `PASS`, no `FAIL`

- [ ] **Step 5: Commit**

```bash
git add src/uptox/services/tick_handler.py src/uptox/quant/runner.py
git commit -m "feat(quant): wire QuantRunner into tick_handler's bar-close event"
```

---

### Task 10: Backtest replay — quant_replay.py

**Files:**
- Create: `src/uptox/backtest/quant_replay.py`
- Test: `tests/test_quant_replay.py`

**Interfaces:**
- Consumes: `Strategy`, `QuantEngine`, `QuantTradingMode`, `book.stats` (Tasks 1, 7, 3, 2).
- Produces: `replay(strategy: Strategy, jsonl_paths: list[Path], capital: float = 20_000.0) -> dict` — runs the strategy through `QuantEngine` in forced-paper mode over the given historical snapshot files in chronological order, returns `book.stats()`-shaped dict plus `"trades": list[dict]`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_quant_replay.py
"""Backtest replay drives the exact same Strategy + QuantEngine code used
live over historical logs/market_data/*.jsonl rows, always in forced-paper
mode — so a backtest number is a real prediction of live behavior, not an
approximation (fixes the limitation backtest/engine.py's docstring flags:
it estimates premium via delta x spot move because it predates recorded
option-chain data)."""
import json
import tempfile
from pathlib import Path

from uptox.quant.strategy import Signal, Strategy
from uptox.backtest.quant_replay import replay


class _AlwaysCEOnFirstBar(Strategy):
    strategy_id = "test_replay"

    def generate_signal(self, history):
        if len(history) == 1:
            return Signal(option="CE", reason="always_first", score=1.0,
                          strategy_id=self.strategy_id)
        return None


def _write_jsonl(rows: list[dict]) -> Path:
    p = Path(tempfile.mkstemp(suffix=".jsonl")[1])
    with p.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    return p


def test_replay_forces_paper_mode_even_if_promoted():
    from uptox.services import order_service
    calls = []
    order_service.schedule_entry_with_sl = lambda *a, **kw: calls.append(a)
    rows = [
        {"ts": "2026-07-20T09:15:00+05:30", "nifty": 24200, "ce_ltp": 150.0, "pe_ltp": 80.0, "sma_score": 0},
        {"ts": "2026-07-20T09:20:00+05:30", "nifty": 24200, "ce_ltp": 145.0, "pe_ltp": 80.0, "sma_score": 0},
        {"ts": "2026-07-20T09:25:00+05:30", "nifty": 24200, "ce_ltp": 139.0, "pe_ltp": 80.0, "sma_score": 0},
    ]
    result = replay(_AlwaysCEOnFirstBar(), [_write_jsonl(rows)])
    assert calls == [], "replay must never touch order_service, even if the strategy's real mode is live"
    assert result["total"] == 1
    assert result["trades"][0]["exit_reason"] == "stop"


def test_replay_reads_multiple_files_in_order():
    rows_day1 = [{"ts": "2026-07-20T09:15:00+05:30", "nifty": 24200, "ce_ltp": 150.0, "pe_ltp": 80.0, "sma_score": 0}]
    rows_day2 = [
        {"ts": "2026-07-21T09:15:00+05:30", "nifty": 24200, "ce_ltp": 155.0, "pe_ltp": 80.0, "sma_score": 0},
        {"ts": "2026-07-21T09:20:00+05:30", "nifty": 24200, "ce_ltp": 171.0, "pe_ltp": 80.0, "sma_score": 0},
    ]
    result = replay(_AlwaysCEOnFirstBar(), [_write_jsonl(rows_day1), _write_jsonl(rows_day2)])
    # signal fires on the 2nd bar seen overall (len(history)==1 means first bar of
    # a fresh position-less strategy call — file 1's single row triggers no signal
    # since it IS bar 1, position opens; file 2 continues the same running history)
    assert result["total"] >= 1


def test_replay_empty_files_returns_zero_trades():
    result = replay(_AlwaysCEOnFirstBar(), [_write_jsonl([])])
    assert result["total"] == 0
    assert result["trades"] == []
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run python -c "
import sys; sys.path.insert(0, 'tests'); sys.path.insert(0, 'src')
import test_quant_replay as t
t.test_replay_empty_files_returns_zero_trades()
"
```
Expected: `ModuleNotFoundError: No module named 'uptox.backtest.quant_replay'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/uptox/backtest/quant_replay.py
"""Replays historical logs/market_data/*.jsonl rows through the exact same
Strategy + QuantEngine code used live, always in forced-paper mode — so a
backtest number is a genuine prediction of live behavior instead of an
approximation. This supersedes backtest/engine.py's premium approximation
(delta x spot move) for quant strategies: real ce_ltp/pe_ltp/oi/iv/pcr are
now recorded per bar and used directly.
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from uptox.quant import book
from uptox.quant.engine import QuantEngine
from uptox.quant.quant_trading_mode import QuantTradingMode
from uptox.quant.strategy import Strategy


def _read_rows(path: Path) -> list[dict]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def replay(strategy: Strategy, jsonl_paths: list[Path], capital: float = 20_000.0) -> dict:
    """Runs `strategy` through a fresh QuantEngine over every row of every
    path in `jsonl_paths`, in the order given (caller sorts paths
    chronologically — typically sorted(Path("logs/market_data").glob("*.jsonl"))).
    Always forced to paper mode, regardless of that strategy_id's real
    promoted mode, so a backtest run can never place a live order."""
    forced_paper_mode = QuantTradingMode(f"replay_{strategy.strategy_id}")
    forced_paper_mode._file = Path(tempfile.mkstemp(suffix=".json")[1])
    forced_paper_mode._mode = "paper"

    book_path = Path(tempfile.mkstemp(suffix=".jsonl")[1])
    engine = QuantEngine(strategy, book_path, forced_paper_mode, capital=capital)

    history: list[dict] = []
    for path in jsonl_paths:
        for row in _read_rows(path):
            history.append(row)
            engine.on_snapshot(row, history)

    trades = book.closed_trades(book_path)
    result = book.stats(trades)
    result["trades"] = trades
    return result
```

- [ ] **Step 4: Run test to verify it passes**

```bash
uv run python -c "
import sys; sys.path.insert(0, 'tests'); sys.path.insert(0, 'src')
import test_quant_replay as t
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
Expected: 3 `PASS` lines, `failures: 0`

- [ ] **Step 5: Commit**

```bash
git add src/uptox/backtest/quant_replay.py tests/test_quant_replay.py
git commit -m "feat(backtest): add quant_replay — real-premium backtest for quant strategies"
```

---

### Task 11: Run the real backtest comparison on recorded data

**Files:**
- None created — this task exercises Task 10 against the real 20 days of `logs/market_data/*.jsonl` to produce the actual comparison numbers.

**Interfaces:**
- Consumes: `replay()` (Task 10), `DirectionalStrategy` (Task 5), `OptionsDynamicsStrategy` (Task 6).
- Produces: a printed comparison report. No new source file.

- [ ] **Step 1: Run the comparison**

```bash
uv run python -c "
import sys; sys.path.insert(0, 'src')
from pathlib import Path
from uptox.quant.directional_strategy import DirectionalStrategy
from uptox.quant.options_dynamics_strategy import OptionsDynamicsStrategy
from uptox.backtest.quant_replay import replay

paths = sorted(Path('logs/market_data').glob('*.jsonl'))
print(f'Replaying {len(paths)} days of recorded data\n')

for strat in (DirectionalStrategy(), OptionsDynamicsStrategy()):
    r = replay(strat, paths)
    print(f'--- {strat.strategy_id} ---')
    print(f'  trades: {r[\"total\"]}  win_rate: {r[\"win_rate\"]}%')
    print(f'  total_pnl_rs: {r[\"total_pnl_rs\"]}  expectancy_rs: {r[\"expectancy_rs\"]}')
    print()
"
```

- [ ] **Step 2: Record the output in the plan's execution log**

Whoever runs this step must paste the actual printed numbers into the PR/commit description or a follow-up message — these are the real results the design spec's "review, judge by expectancy" step depends on. Do not fabricate or estimate them.

- [ ] **Step 3: No commit** — this task produces no file changes, only output to report.

---

### Task 12: web/quant_data.py — read helpers for the dashboard

**Files:**
- Create: `src/uptox/web/quant_data.py`
- Test: `tests/test_quant_web_data.py`

**Interfaces:**
- Consumes: `book.closed_trades`, `book.stats` (Task 2).
- Produces: `get_trades(strategy_id: str) -> list[dict]` (newest first), `get_stats(strategy_id: str) -> dict`. `strategy_id` is `"directional"` or `"options_dynamics"`, mapped to `logs/quant_{strategy_id}_trades.jsonl` — note the directional book file from Task 8 is `logs/quant_directional_trades.jsonl` and the options one is `logs/quant_options_trades.jsonl` (not `quant_options_dynamics_trades.jsonl` — keep the short name already used by `runner.py`).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_quant_web_data.py
"""Web read layer for the quant dashboard — re-reads the JSONL book files
fresh on every call (same convention as web/data.py, since the web
process is separate from the live trading process)."""
import json
import tempfile
from pathlib import Path

from uptox.web import quant_data


def test_get_trades_unknown_strategy_returns_empty():
    quant_data._BOOK_PATHS["directional"] = Path("logs/does_not_exist_xyz.jsonl")
    assert quant_data.get_trades("directional") == []


def test_get_trades_reads_and_orders_newest_first():
    p = Path(tempfile.mkstemp(suffix=".jsonl")[1])
    with p.open("w", encoding="utf-8") as f:
        f.write(json.dumps({"event": "open", "id": "a1", "option": "CE", "entry_price": 100.0}) + "\n")
        f.write(json.dumps({"event": "close", "id": "a1", "exit_price": 110.0, "pnl_pts": 10.0, "pnl_rs": 6500.0}) + "\n")
        f.write(json.dumps({"event": "open", "id": "a2", "option": "PE", "entry_price": 90.0}) + "\n")
        f.write(json.dumps({"event": "close", "id": "a2", "exit_price": 80.0, "pnl_pts": 10.0, "pnl_rs": 6500.0}) + "\n")
    quant_data._BOOK_PATHS["directional"] = p
    trades = quant_data.get_trades("directional")
    assert len(trades) == 2
    assert trades[0]["id"] == "a2"  # newest first


def test_get_stats_matches_book_stats():
    p = Path(tempfile.mkstemp(suffix=".jsonl")[1])
    with p.open("w", encoding="utf-8") as f:
        f.write(json.dumps({"event": "open", "id": "a1", "option": "CE", "entry_price": 100.0}) + "\n")
        f.write(json.dumps({"event": "close", "id": "a1", "exit_price": 110.0, "pnl_pts": 10.0, "pnl_rs": 6500.0}) + "\n")
    quant_data._BOOK_PATHS["options_dynamics"] = p
    s = quant_data.get_stats("options_dynamics")
    assert s["total"] == 1
    assert s["total_pnl_rs"] == 6500.0
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run python -c "
import sys; sys.path.insert(0, 'tests'); sys.path.insert(0, 'src')
import test_quant_web_data as t
t.test_get_trades_unknown_strategy_returns_empty()
"
```
Expected: `ModuleNotFoundError: No module named 'uptox.web.quant_data'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/uptox/web/quant_data.py
"""Read-only data access for the quant dashboard panels. Runs in the web
process, separate from the live trading process, so it re-reads the JSONL
book files fresh on every call — same convention as web/data.py."""
from __future__ import annotations

from pathlib import Path

from uptox.quant import book

_BOOK_PATHS: dict[str, Path] = {
    "directional":      Path("logs/quant_directional_trades.jsonl"),
    "options_dynamics": Path("logs/quant_options_trades.jsonl"),
}


def get_trades(strategy_id: str) -> list[dict]:
    path = _BOOK_PATHS.get(strategy_id)
    if not path:
        return []
    trades = book.closed_trades(path)
    return list(reversed(trades))


def get_stats(strategy_id: str) -> dict:
    path = _BOOK_PATHS.get(strategy_id)
    if not path:
        return book.stats([])
    return book.stats(book.closed_trades(path))
```

- [ ] **Step 4: Run test to verify it passes**

```bash
uv run python -c "
import sys; sys.path.insert(0, 'tests'); sys.path.insert(0, 'src')
import test_quant_web_data as t
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
Expected: 3 `PASS` lines, `failures: 0`

- [ ] **Step 5: Commit**

```bash
git add src/uptox/web/quant_data.py tests/test_quant_web_data.py
git commit -m "feat(web): add quant_data read layer for dashboard panels"
```

---

### Task 13: web/app.py — quant API routes

**Files:**
- Modify: `src/uptox/web/app.py` (add routes after the existing `/api/stats` route, around line 176)

**Interfaces:**
- Consumes: `quant_data.get_trades`, `quant_data.get_stats` (Task 12).
- Produces: `GET /api/quant/{strategy_id}/trades`, `GET /api/quant/{strategy_id}/stats`. `strategy_id` path param restricted to `directional` / `options_dynamics` (404 on anything else, matching FastAPI's `Literal` path-param validation).

- [ ] **Step 1: Add the routes**

Insert into `src/uptox/web/app.py` after the existing `api_stats` route (after line 176's block, before the notifications routes):

```python
from typing import Literal
from uptox.web import quant_data

_QuantStrategy = Literal["directional", "options_dynamics"]


@app.get("/api/quant/{strategy_id}/trades", summary="Quant strategy trade history", tags=["quant"])
def api_quant_trades(strategy_id: _QuantStrategy):
    """Closed trades for one quant strategy, newest first."""
    return quant_data.get_trades(strategy_id)


@app.get("/api/quant/{strategy_id}/stats", summary="Quant strategy stats", tags=["quant"])
def api_quant_stats(strategy_id: _QuantStrategy):
    """Win rate, expectancy, total P&L for one quant strategy."""
    return quant_data.get_stats(strategy_id)
```

(`Literal` import: check the top of `app.py` first — if `from typing import Literal` or similar is already present, don't duplicate it; add only what's missing.)

- [ ] **Step 2: Manual smoke check — app starts and routes resolve**

```bash
uv run python -c "
import sys; sys.path.insert(0, 'src')
from uptox.web.app import app
paths = [r.path for r in app.routes]
assert '/api/quant/{strategy_id}/trades' in paths
assert '/api/quant/{strategy_id}/stats' in paths
print('OK — quant routes registered')
"
```
Expected: `OK — quant routes registered`

- [ ] **Step 3: Manual smoke check — routes actually respond**

```bash
uv run python -c "
import sys; sys.path.insert(0, 'src')
from fastapi.testclient import TestClient
from uptox.web.app import app
c = TestClient(app)
r1 = c.get('/api/quant/directional/stats')
r2 = c.get('/api/quant/bogus/stats')
print('directional stats status:', r1.status_code, r1.json())
print('bogus strategy status:', r2.status_code)
assert r1.status_code == 200
assert r2.status_code == 422  # FastAPI Literal validation rejects unknown values
print('OK')
"
```
Expected: `directional stats status: 200 {...}`, `bogus strategy status: 422`, `OK`. If `fastapi.testclient` import fails (no `httpx`/`starlette` test extras), skip Step 3 and rely on Step 2 plus manual `curl` against `uv run uptox-web` instead — note which path was used when reporting completion.

- [ ] **Step 4: Commit**

```bash
git add src/uptox/web/app.py
git commit -m "feat(web): add /api/quant/{strategy_id}/trades and /stats routes"
```

---

### Task 14: ui/screens/quant_book.py — Textual dashboard panel

**Files:**
- Create: `src/uptox/ui/screens/quant_book.py`
- Modify: `src/uptox/ui/app.py` (register the new screen, mirroring how `TradeBookScreen` is registered around line 6/45)

**Interfaces:**
- Consumes: `quant_data.get_trades`, `quant_data.get_stats` (Task 12) — reused directly (the Textual app runs in-process, unlike the separate web process, but the same read functions work fine since they just re-read the JSONL files).
- Produces: `QuantBookScreen` widget, tab-switchable between the two strategy books, following the exact structure of `TradeBookScreen` (`src/uptox/ui/screens/trade_book.py`).

- [ ] **Step 1: Write the screen**

```python
# src/uptox/ui/screens/quant_book.py
"""Quant Book screen — tab per strategy (Directional | Options Dynamics),
each showing running stats + trade history, so focus mode and both quant
strategies can be compared side by side. Mirrors trade_book.py's
TabbedContent structure."""
from textual.app import ComposeResult
from textual.containers import ScrollableContainer
from textual.widget import Widget
from textual.widgets import Static, TabbedContent, TabPane, DataTable, Label
from uptox.web import quant_data

_OUTCOME_COLOR = {"WIN": "green", "LOSS": "red"}


class _StrategyStatsPanel(Static):
    def __init__(self, strategy_id: str, **kwargs):
        super().__init__(**kwargs)
        self.strategy_id = strategy_id

    def on_mount(self) -> None:
        self._refresh()
        self.set_interval(2.0, self._refresh)

    def _refresh(self) -> None:
        s = quant_data.get_stats(self.strategy_id)
        if s["total"] == 0:
            self.update(f"[bold cyan]▸ {self.strategy_id}[/]\n\n[dim white]No closed trades yet.[/]")
            return
        pnl_col = "green" if s["total_pnl_rs"] >= 0 else "red"
        exp_col = "green" if s["expectancy_rs"] >= 0 else "red"
        wr_col  = "green" if s["win_rate"] >= 55 else "yellow" if s["win_rate"] >= 45 else "red"
        self.update(
            f"[bold cyan]▸ {self.strategy_id}[/]\n\n"
            f"[dim]{'Trades':<16}[/] [white]{s['total']}[/]  "
            f"[dim]Wins:[/] [green]{s['wins']}[/]  [dim]Losses:[/] [red]{s['losses']}[/]\n"
            f"[dim]{'Win Rate':<16}[/] [{wr_col}]{s['win_rate']}%[/]\n"
            f"[dim]{'Total P&L':<16}[/] [{pnl_col}]{s['total_pnl_pts']:+.1f} pts   ₹{s['total_pnl_rs']:+,.0f}[/]\n"
            f"[dim]{'Expectancy':<16}[/] [{exp_col}]{s['expectancy_pts']:+.1f} pts   ₹{s['expectancy_rs']:+,.0f} /trade[/]"
        )


class _StrategyHistoryPanel(Widget):
    DEFAULT_CSS = """
    _StrategyHistoryPanel { height: 1fr; width: 100%; layout: vertical; }
    _StrategyHistoryPanel DataTable { height: 1fr; width: 100%; overflow-x: auto; overflow-y: auto; }
    """

    def __init__(self, strategy_id: str, **kwargs):
        super().__init__(**kwargs)
        self.strategy_id = strategy_id

    def compose(self) -> ComposeResult:
        yield Label("")
        yield DataTable(show_cursor=True)

    def on_mount(self) -> None:
        tbl = self.query_one(DataTable)
        tbl.add_columns("#", "Date", "Opt", "Entry", "Exit", "P&L pts", "P&L ₹", "Reason")
        self._refresh()
        self.set_interval(2.0, self._refresh)

    def _refresh(self) -> None:
        trades = quant_data.get_trades(self.strategy_id)[:20]
        self.query_one(Label).update(f"[dim](last {len(trades)} trades)[/]")
        tbl = self.query_one(DataTable)
        tbl.clear()
        for i, t in enumerate(trades, 1):
            pnl_pts = t.get("pnl_pts", 0.0)
            col = "green" if pnl_pts >= 0 else "red"
            tbl.add_row(
                str(i), t.get("date", "—"), t.get("option", "—"),
                f"{t.get('entry_price', 0):.1f}", f"{t.get('exit_price', 0):.1f}",
                f"[{col}]{pnl_pts:+.1f}[/]", f"[{col}]{t.get('pnl_rs', 0):+,.0f}[/]",
                t.get("exit_reason", "—"),
            )


class QuantBookScreen(ScrollableContainer):
    DEFAULT_CSS = """
    QuantBookScreen { height: 1fr; }
    QuantBookScreen TabbedContent { height: 1fr; }
    QuantBookScreen TabPane { height: 1fr; overflow-y: auto; overflow-x: auto; padding: 0; }
    """

    def compose(self) -> ComposeResult:
        with TabbedContent(initial="qb-directional"):
            with TabPane("Directional", id="qb-directional"):
                yield _StrategyStatsPanel("directional")
                yield _StrategyHistoryPanel("directional")
            with TabPane("Options Dynamics", id="qb-options"):
                yield _StrategyStatsPanel("options_dynamics")
                yield _StrategyHistoryPanel("options_dynamics")
```

- [ ] **Step 2: Register the screen in ui/app.py**

Read the current registration block first:
```bash
uv run python -c "
lines = open('src/uptox/ui/app.py', encoding='utf-8').readlines()
print(''.join(lines[:50]))
"
```

Add the import next to the existing screen imports (near line 6):
```python
from uptox.ui.screens.quant_book import QuantBookScreen
```

Add the tab/pane next to `TradeBookScreen`'s registration (mirror the existing `with TabPane(...): yield TradeBookScreen()` block — exact insertion point depends on what Step 2's read shows; add a new `TabPane("Quant Book", id="tab-quant")` yielding `QuantBookScreen()` immediately after the Trade Book tab, following the same pattern as the other four screens).

- [ ] **Step 3: Manual smoke check — screen imports without error**

```bash
uv run python -c "
import sys; sys.path.insert(0, 'src')
from uptox.ui.screens.quant_book import QuantBookScreen
from uptox.ui.app import App
print('OK — QuantBookScreen and App import cleanly')
"
```
Expected: `OK — QuantBookScreen and App import cleanly`

- [ ] **Step 4: Manual verification — run the app and check the tab**

```bash
uv run src/uptox/main.py
```
Switch to the new Quant Book tab, confirm both sub-tabs (Directional / Options Dynamics) render without a traceback, showing "No closed trades yet." (expected — no live/backtest trades have been recorded into these book files yet at this point in the plan). This step cannot be automated — Textual screens require a real terminal render; report this as manually verified when done, not test-covered.

- [ ] **Step 5: Commit**

```bash
git add src/uptox/ui/screens/quant_book.py src/uptox/ui/app.py
git commit -m "feat(ui): add Quant Book dashboard screen with per-strategy tabs"
```

---

## Post-plan: what's still deliberately out of scope

- ML-based signal generation (v2, once more data accumulates) — per design spec.
- Promotion of either strategy to live order placement — a separate, later, explicit decision made after reviewing Task 11's real backtest numbers and some paper-forward days.
- Daily loss limits / max-trades-per-day guards for the quant books (focus mode has these via `capital_service`; the quant engine doesn't yet) — not in the approved spec, flag if promotion to live is considered later.
