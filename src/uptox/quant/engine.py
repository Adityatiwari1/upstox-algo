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
LOCK_KEEP     = 0.65   # was 0.50 — 21-day replay sweep 2026-07-24: 65/75 keeps
LOCK_BIG_PTS  = 15.0   # expectancy Rs886 vs Rs841/trade, and the 2026-07-24 live
LOCK_BIG_KEEP = 0.75   # trade gave back 57% of a +14.2pt peak under 50/60
RR            = 2.0
SESSION_CLOSE = (15, 20)
MARKET_OPEN   = (9, 15)  # F&O has no pre-open auction -- nothing before this is a real tradable price


def _bar_time(snapshot: dict) -> datetime:
    """The bar's own time, not wall-clock — keeps backtest replay reproducible
    regardless of when it's run. Falls back to wall-clock only if the
    snapshot has no "ts" (live snapshots from data_recorder.py always do)."""
    ts = snapshot.get("ts")
    if ts:
        try:
            return datetime.fromisoformat(ts)
        except ValueError:
            pass
    return datetime.now(_IST)


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
        bar_time = _bar_time(snapshot)
        if (bar_time.hour, bar_time.minute) < MARKET_OPEN:
            return  # F&O isn't open yet -- this snapshot's prices aren't real

        entry = snapshot.get("ce_ltp") if signal.option == "CE" else snapshot.get("pe_ltp")
        if not entry or entry <= 0:
            return
        sl     = round(entry - STOP_PTS, 1)
        target = round(entry + STOP_PTS * RR, 1)
        lots   = _size_lots(self.capital, entry, sl)
        if lots <= 0:
            return

        pos_id = f"{signal.strategy_id}-{bar_time.strftime('%H%M%S%f')}"
        # Pin the actual contract when the live option chain can resolve it —
        # live ticks for that key then manage the exit at tick resolution
        # (see on_tick) and keep tracking the SAME contract through ATM
        # rolls. In backtest there is no chain, get_key returns None, and
        # the bar-snapshot path in _check_exit manages exits as before.
        try:
            from uptox.services import option_chain_service
            _pin_key = (option_chain_service.get_key(snapshot.get("atm"), signal.option)
                        if snapshot.get("atm") else None)
        except Exception:
            _pin_key = None
        self.position = {
            "id": pos_id, "date": bar_time.strftime("%Y-%m-%d"),
            "strategy_id": signal.strategy_id, "option": signal.option,
            "entry_price": entry, "spot_entry": snapshot.get("nifty", 0.0),
            "entry_strike": snapshot.get("atm"), "option_key": _pin_key,
            "reason": signal.reason, "lots": lots, "sl": sl, "target": target,
            "peak": entry, "last_valid_ltp": entry,
        }
        book.append_event(self.book_path, {
            "event": "open", "ts": datetime.now(_IST).isoformat(), "id": pos_id,
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
            else:
                log.warning("[quant:%s] live mode but option_chain_service.get_key(%s, %s) returned no key — no broker order placed",
                            signal.strategy_id, atm, signal.option)

    def _check_exit(self, snapshot: dict) -> None:
        pos = self.position

        # A position pinned to a real contract (option_key) is exit-managed
        # by live ticks in on_tick — this bar's rolling-ATM ce_ltp/pe_ltp
        # may already be a different contract's price, so the snapshot path
        # must not touch a pinned position at all.
        if pos.get("option_key"):
            return

        # data_recorder only ever records the CURRENT-ATM CE/PE -- once the
        # strike we're actually holding stops being ATM, this bar's
        # ce_ltp/pe_ltp is a different contract's price. Force-close at the
        # last price we know is genuinely the held contract's rather than
        # mis-marking the position to an unrelated strike (2026-07-23 bug:
        # a 24150 CE was marked to the new 23900 ATM CE's price, recording
        # a fake +114.2pt "target hit").
        cur_atm = snapshot.get("atm")
        entry_strike = pos.get("entry_strike")
        if cur_atm is not None and entry_strike is not None and cur_atm != entry_strike:
            self._close(pos["last_valid_ltp"], "atm_rolled")
            return

        ltp = snapshot.get("ce_ltp") if pos["option"] == "CE" else snapshot.get("pe_ltp")
        if not ltp or ltp <= 0:
            return
        pos["last_valid_ltp"] = ltp
        if ltp > pos["peak"]:
            pos["peak"] = ltp
        self._apply_exits(ltp, _bar_time(snapshot))

    def on_tick(self, instrument_key: str, ltp: float,
                now: datetime | None = None) -> None:
        """Tick-resolution exit management for a pinned position. Bar
        snapshots lag exits by up to 5 minutes (the 2026-07-24 live trade
        closed at 138.9 against its own 139.9 lock floor purely from bar
        lag); ticks for the pinned contract fix that, and keep following
        the SAME contract through ATM rolls. `now` is injectable for tests;
        live callers omit it (wall clock)."""
        pos = self.position
        if pos is None or not ltp or pos.get("option_key") != instrument_key:
            return
        # Freak-tick damper (mirrors shadow_book): ignore one isolated >15%
        # jump — a real repricing shows up on the next tick too.
        prev = pos.get("last_valid_ltp")
        if prev and abs(ltp - prev) / prev > 0.15 and not pos.get("suspect_seen"):
            pos["suspect_seen"] = True
            return
        pos["suspect_seen"] = False
        pos["last_valid_ltp"] = ltp
        if ltp > pos["peak"]:
            pos["peak"] = ltp
        self._apply_exits(ltp, now or datetime.now(_IST))

    def _apply_exits(self, ltp: float, now: datetime) -> None:
        pos = self.position
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
            else:
                log.warning("[quant:%s] live mode but option_chain_service.get_key(%s, %s) returned no key — no broker order placed",
                            pos["strategy_id"], atm, pos["option"])
