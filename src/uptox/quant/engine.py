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
