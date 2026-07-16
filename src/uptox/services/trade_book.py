"""
Paper trading book.
Auto-records every system trade signal as a paper trade.
Persists to logs/trades.json on every close.
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import datetime, time as dt_time
from pathlib import Path
from zoneinfo import ZoneInfo

try:
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment
    _XLSX_OK = True
except ImportError:
    _XLSX_OK = False

_IST              = ZoneInfo("Asia/Kolkata")
_SESSION_CLOSE    = dt_time(15, 25)   # force-exit ALL trades at 3:25 PM IST

# A trade only counts as a "real" loss for behavioral blocks (direction
# lockout, consecutive-loss pause, post-loss cooldown) when it loses at
# least this many premium points. Scratches (-0.1/-0.5pt trail exits) are
# not wrong directional reads — on 2026-07-08 three scratch "losses" paused
# the system for the day during a -430pt crash it had called correctly.
REAL_LOSS_PTS = -5.0
# Symmetrically, only meaningful winners count toward the 2-win "protect
# profits" stop — with the profit lock turning scratches into small wins,
# two +4pt exits must not pause a day whose real move is still coming.
REAL_WIN_PTS = 8.0

log = logging.getLogger(__name__)

_TRADES_FILE     = Path("logs/trades.json")
_TRADES_XLSX     = Path("logs/trades.xlsx")
_OPEN_TRADE_FILE = Path("logs/open_trade.json")   # persists open trade across restarts
_trades: list[dict] = []
_open_trade: dict | None = None
_daily_pnl_reset_rs:    float = 0.0   # P&L baseline at last Z-reset; subtract to restart loss counter
_daily_trades_reset_n:  int   = 0     # trade count at last Z-reset; subtract to give fresh trade slots
_slots_logged:          int   = -1    # last logged trade-slot count (log only on change)

_XLSX_COLS = [
    ("Date",         "date"),
    ("Entry Time",   "time_entry"),
    ("Exit Time",    "time_exit"),
    ("Strike",       "strike"),
    ("Option",       "option"),
    ("Expiry",       "expiry"),
    ("Entry ₹",      "entry_price"),
    ("Exit ₹",       "exit_price"),
    ("Lots",         "lots"),
    ("Invested",     "invested"),
    ("SL",           "sl"),
    ("Target",       "target"),
    ("Delta",        "delta"),
    ("Spot Entry",   "spot_entry"),
    ("PnL Pts",      "pnl_pts"),
    ("PnL ₹",        "pnl_rs"),
    ("Outcome",      "outcome"),
    ("Entry Reason", "entry_reason"),
    ("Exit Reason",  "exit_reason"),
    ("Score",        "score"),
    ("SMA Score",    "sma_score"),
    ("Flow Score",   "flow_score"),
    ("ATR Entry",    "atr_at_entry"),
    ("Expiry Day",   "expiry_day"),
]


# ── persistence ───────────────────────────────────────────────────────────────

_last_open_save: float = 0.0
_OPEN_SAVE_INTERVAL = 5.0   # seconds — throttle live-update writes to disk

def _save_open(force: bool = False) -> None:
    """Persist open trade to disk so restarts can recover it.
    Throttled to once per 5s for live tick updates; pass force=True on open/close."""
    global _last_open_save
    now = time.monotonic()
    if not force and (now - _last_open_save) < _OPEN_SAVE_INTERVAL:
        return
    _last_open_save = now
    try:
        _OPEN_TRADE_FILE.parent.mkdir(exist_ok=True)
        if _open_trade:
            data = dict(_open_trade)
            # Snapshot live trail state — restart recovery must restore the
            # earned peak/floor, not reset the profit lock to entry defaults
            from uptox.services import trailing_sl_service
            ts = trailing_sl_service.get_status()
            if ts.get("active"):
                data["trail_peak"]     = ts["peak"]
                data["trail_sl_floor"] = ts["sl_floor"]
            _OPEN_TRADE_FILE.write_text(
                json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
            )
        elif _OPEN_TRADE_FILE.exists():
            _OPEN_TRADE_FILE.unlink(missing_ok=True)
    except Exception as e:
        log.error("open_trade save error: %s", e)


def _load() -> None:
    global _trades, _open_trade
    if _TRADES_FILE.exists():
        try:
            _trades = json.loads(_TRADES_FILE.read_text(encoding="utf-8"))
        except Exception as e:
            log.error("trade_book load error: %s", e)
            _trades = []
    # Restore any open trade that survived a restart
    if _OPEN_TRADE_FILE.exists():
        try:
            raw = _OPEN_TRADE_FILE.read_text(encoding="utf-8").strip()
            if raw:
                data = json.loads(raw)
                if data.get("outcome") == "OPEN":
                    _open_trade = data
                    ep      = float(data.get("entry_price", 0))
                    sl      = float(data.get("sl", 0))
                    opt     = data.get("option", "")
                    opt_key = data.get("option_key", "")
                    peak_saved  = float(data.pop("trail_peak", 0.0))
                    floor_saved = float(data.pop("trail_sl_floor", 0.0))
                    if ep:
                        from uptox.services import trailing_sl_service
                        from uptox.services.capital_service import is_expiry_afternoon, is_expiry_day
                        tgt = float(data.get("target", 0))
                        trailing_sl_service.set_entry(
                            ep, opt, opt_key, target=tgt, initial_sl=sl,
                            expiry_afternoon=is_expiry_afternoon(),
                            expiry_day=is_expiry_day(),
                            regime=data.get("regime", "NORMAL"),
                            atr_pts=float(data.get("atr_at_entry", 0) or 0),
                        )
                        if peak_saved or floor_saved:
                            trailing_sl_service.restore_state(
                                peak=peak_saved, sl_floor=floor_saved,
                            )
                    log.info(
                        "Restored open trade from disk: %s %s @ %.1f  SL=%.1f  Peak=%.1f  SL_Floor=%.1f",
                        opt, data.get("strike"), ep, sl, peak_saved or ep, floor_saved or sl,
                    )
        except Exception as e:
            log.error("open_trade restore error: %s", e)
            _open_trade = None

    # Sync capital to base + all historical realised P&L
    from uptox.services import capital_service
    capital_service.sync_from_history(_trades)


def _save() -> None:
    try:
        _TRADES_FILE.parent.mkdir(exist_ok=True)
        _TRADES_FILE.write_text(
            json.dumps(_trades, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    except Exception as e:
        log.error("trade_book save error: %s", e)
    if _XLSX_OK:
        _save_xlsx()


def _save_xlsx() -> None:
    try:
        _TRADES_XLSX.parent.mkdir(exist_ok=True)
        if _TRADES_XLSX.exists():
            wb = openpyxl.load_workbook(_TRADES_XLSX)
            ws = wb.active
        else:
            wb = openpyxl.Workbook()
            ws = wb.active
            ws.title = "Trades"
            # Header row
            headers = [col for col, _ in _XLSX_COLS]
            ws.append(headers)
            hdr_fill   = PatternFill("solid", fgColor="1F2D3D")
            hdr_font   = Font(bold=True, color="FFFFFF")
            hdr_align  = Alignment(horizontal="center")
            for cell in ws[1]:
                cell.fill  = hdr_fill
                cell.font  = hdr_font
                cell.alignment = hdr_align
            # Column widths
            widths = [12, 11, 11, 8, 7, 12, 9, 9, 6, 10, 9, 9, 7, 11, 9, 10, 14, 18, 20, 7, 10, 11, 10]
            for i, w in enumerate(widths, 1):
                ws.column_dimensions[openpyxl.utils.get_column_letter(i)].width = w

        # Append the latest closed trade
        if not _trades:
            return
        t = _trades[-1]
        row = [t.get(field) for _, field in _XLSX_COLS]
        ws.append(row)

        # Colour the row by outcome
        outcome = t.get("outcome", "")
        if outcome in ("TARGET_HIT", "WIN"):
            fill = PatternFill("solid", fgColor="1A3A1A")
            font_color = "00CC44"
        elif outcome in ("SL_HIT", "LOSS"):
            fill = PatternFill("solid", fgColor="3A1A1A")
            font_color = "FF4444"
        else:
            fill = PatternFill("solid", fgColor="2A2A2A")
            font_color = "AAAAAA"

        last_row = ws.max_row
        for cell in ws[last_row]:
            cell.fill      = fill
            cell.font      = Font(color=font_color)
            cell.alignment = Alignment(horizontal="center")

        wb.save(_TRADES_XLSX)
        log.info("Trade saved to %s", _TRADES_XLSX)
    except Exception as e:
        log.error("trade_book xlsx save error: %s", e)


def should_session_close() -> bool:
    """True at or after 3:30 PM IST — force-exit ALL open trades, end of session."""
    if not _open_trade:
        return False
    return datetime.now(_IST).time() >= _SESSION_CLOSE


# ── public API ────────────────────────────────────────────────────────────────

def open_trade(
    *,
    strike: int,
    option: str,
    expiry: str,
    entry_price: float,
    lots: int,
    invested: float,
    sl: float,
    target: float,
    option_key: str = "",
    delta: float | None = None,
    spot: float = 0.0,
    entry_reason: str = "",
    score: int = 0,
    sma_score: int = 0,
    flow_score: int = 0,
    atr_at_entry: float = 0.0,
    expiry_day: bool = False,
    regime: str = "NORMAL",
) -> None:
    global _open_trade
    if _open_trade:
        return  # already in a trade
    from uptox.services.capital_service import NIFTY_LOT_SIZE
    _open_trade = {
        "id":          str(uuid.uuid4())[:8],
        "date":        datetime.now(_IST).strftime("%Y-%m-%d"),
        "time_entry":  datetime.now(_IST).strftime("%H:%M:%S"),
        "entry_ts":    time.time(),
        "time_exit":   None,
        "strike":      strike,
        "option":      option,
        "expiry":      expiry,
        "option_key":  option_key,
        "entry_price": round(entry_price, 1),
        "exit_price":  None,
        "lots":        lots,
        "qty":         lots * NIFTY_LOT_SIZE,
        "lot_size":    NIFTY_LOT_SIZE,
        "invested":    round(invested, 0),
        "sl":          round(sl, 1),
        "target":      round(target, 1),
        "delta":       round(delta, 3) if delta else None,
        "spot_entry":  round(spot, 1),
        "spot_current": round(spot, 1),
        "spot_peak":   round(spot, 1),
        "current_price": entry_price,
        "pnl_pts":     0.0,
        "pnl_rs":      0.0,
        "outcome":      "OPEN",
        "exit_reason":  None,
        "entry_reason": entry_reason,
        "score":        score,
        "sma_score":    sma_score,
        "flow_score":   flow_score,
        "atr_at_entry": round(atr_at_entry, 1),
        "expiry_day":   expiry_day,
        "regime":       regime,
    }
    from uptox.services import trailing_sl_service
    from uptox.services.capital_service import is_expiry_afternoon, is_expiry_day
    ea = is_expiry_afternoon()
    trailing_sl_service.set_entry(
        entry_price, option, option_key,
        target=target, initial_sl=sl,
        expiry_afternoon=ea,
        expiry_day=is_expiry_day(),
        regime=regime,
        atr_pts=atr_at_entry,
    )
    log.info(
        "Paper OPEN%s: %s %s @ %.1f  SL=%.1f  TGT=%.1f  lots=%d",
        " [EXPIRY]" if ea else "", option, strike, entry_price, sl, target, lots,
    )
    _save_open(force=True)
    from uptox.services.telegram_service import notify_trade_open
    notify_trade_open(_open_trade)
    # Live order — fire only when mode is live; paper record kept regardless.
    # BUY, then ONE resting SL-M at the broker (exchange allows a single stop
    # order — the trail modifies its trigger via sync_sl_order, never adds one).
    from uptox.services import trading_mode, order_service
    if trading_mode.is_live() and option_key:
        _tid = _open_trade["id"]

        def _store_sl_id(oid: str) -> None:
            if _open_trade and _open_trade.get("id") == _tid:
                _open_trade["sl_order_id"]      = oid
                _open_trade["sl_order_trigger"] = _open_trade["sl"]
                _save_open(force=True)
            else:
                # Trade already closed before the SL-M rested — orphan stop
                # at the broker would fire on a flat position; kill it.
                log.warning("SL-M %s rested after trade %s closed — cancelling", oid, _tid)
                order_service._spawn(order_service.cancel_order(oid))

        order_service.schedule_entry_with_sl(
            option_key, _open_trade["qty"], entry_price, sl,
            on_sl_order_id=_store_sl_id,
        )


def _attach_costs(trade_id: str, option_key: str, qty: int,
                  entry_price: float, exit_price: float) -> None:
    """Fetch the broker's real charges for the round trip (Upstox brokerage
    API) and attach cost_rs / pnl_rs_net to the closed record. Async — the
    record is re-saved when the numbers land. Runs in paper mode too: the
    fees are what they WOULD be, so paper P&L stops flattering itself."""
    if not option_key or qty <= 0 or not exit_price:
        return
    from uptox.services import order_service

    async def _run() -> None:
        buy  = await order_service.get_charges(option_key, qty, entry_price, "BUY")
        sell = await order_service.get_charges(option_key, qty, exit_price, "SELL")
        if buy is None and sell is None:
            return
        cost = round((buy or 0.0) + (sell or 0.0), 2)
        for t in reversed(_trades):
            if t.get("id") == trade_id and t.get("outcome") != "OPEN":
                t["cost_rs"]    = cost
                t["pnl_rs_net"] = round(t.get("pnl_rs", 0.0) - cost, 2)
                _save()
                # adjust_for_pnl(pnl_rs) already ran gross at close_trade() —
                # true up the live capital pool now that the real fee is known
                from uptox.services import capital_service
                capital_service.adjust_for_pnl(-cost)
                log.info("Charges attached: %s  cost=₹%.2f  net=₹%.2f  capital→₹%.0f",
                         trade_id, cost, t["pnl_rs_net"], capital_service.get_capital())
                break

    try:
        order_service._spawn(_run())
    except Exception as e:
        log.error("charges attach error: %s", e)


def close_trade(exit_price: float, reason: str) -> dict | None:
    global _open_trade
    if not _open_trade:
        return None
    from uptox.services.capital_service import NIFTY_LOT_SIZE

    pnl_pts = round(exit_price - _open_trade["entry_price"], 1)
    pnl_rs  = round(pnl_pts * _open_trade["lots"] * NIFTY_LOT_SIZE, 0)

    # Trail state must be read before trailing_sl_service.exit_trade() below
    from uptox.services import trailing_sl_service as _tsl
    ts = _tsl.get_status()

    # Outcome based purely on exit price — reason is just metadata.
    # SL_HIT checks the LIVE trail floor, not the static entry SL: the floor
    # is what actually stopped the trade out (entry "sl" never moves, so a
    # dynamic-floor stop at a loss used to log as generic LOSS).
    live_sl = ts["sl_floor"] if ts.get("active") else _open_trade["sl"]
    if exit_price >= _open_trade["target"]:
        outcome = "TARGET_HIT"     # closed above target (trail ran past it)
    elif pnl_pts > 0:
        outcome = "WIN"            # trail SL caught profits between SL and target
    elif exit_price <= live_sl and pnl_pts < 0:
        outcome = "SL_HIT"         # stopped out at or below the active floor
    elif pnl_pts < 0:
        outcome = "LOSS"
    else:
        outcome = "BREAKEVEN"

    # Capture peak price / max unrealised profit before trail SL fired
    peak_price  = ts["peak"] if ts.get("active") and ts["peak"] >= _open_trade["entry_price"] else exit_price
    max_pnl_pts = round(peak_price - _open_trade["entry_price"], 1)
    max_pnl_rs  = round(max_pnl_pts * _open_trade["lots"] * NIFTY_LOT_SIZE, 0)

    # Nifty 50 (underlying) points moved during this trade — separate from the
    # option premium pts above. directional_* is signed so + always means the
    # index moved in the trade's favor (up for CE, down for PE), regardless of
    # which way it actually moved on the tape.
    from uptox.services import feed_store
    nifty_tick = feed_store.get("NSE_INDEX|Nifty 50")
    spot_entry = _open_trade.get("spot_entry", 0.0)
    spot_peak  = _open_trade.get("spot_peak", spot_entry)
    spot_exit  = round(nifty_tick.ltp, 1) if nifty_tick and nifty_tick.ltp else _open_trade.get("spot_current", spot_entry)
    favorable  = 1 if _open_trade["option"] == "CE" else -1

    _open_trade.update({
        "exit_price":   round(exit_price, 1),
        "time_exit":    datetime.now(_IST).strftime("%H:%M:%S"),
        "exit_ts":      time.time(),
        "pnl_pts":      pnl_pts,
        "pnl_rs":       pnl_rs,
        "outcome":      outcome,
        "exit_reason":  reason,
        "peak_price":   round(peak_price, 1),
        "max_pnl_pts":  max_pnl_pts,
        "max_pnl_rs":   max_pnl_rs,
        "sl_floor_exit": round(live_sl, 1),   # active trail floor at close ("sl" = initial entry SL)
        "spot_exit":              spot_exit,
        "spot_move_pts":          round(spot_exit - spot_entry, 1),
        "spot_directional_pts":   round((spot_exit - spot_entry) * favorable, 1),
        "spot_peak_move_pts":     round((spot_peak - spot_entry) * favorable, 1),
    })
    closed = dict(_open_trade)
    closed.pop("current_price", None)
    _trades.append(closed)
    _open_trade = None
    _save()
    _save_open(force=True)   # clears open_trade.json — trade is closed
    # Adjust running capital with realised P&L
    from uptox.services import capital_service, trailing_sl_service
    capital_service.adjust_for_pnl(pnl_rs)
    trailing_sl_service.exit_trade()

    log.info(
        "Paper CLOSE: %s  P&L=%.1fpts  ₹%.0f  reason=%s",
        outcome, pnl_pts, pnl_rs, reason,
    )
    _attach_costs(closed["id"], closed.get("option_key", ""), closed.get("qty", 0),
                  closed["entry_price"], closed["exit_price"])
    # Live exit order — fire only when mode is live; paper record kept regardless.
    # Cancels the resting SL-M before selling (skips the sell if it already filled).
    from uptox.services import trading_mode, order_service
    if trading_mode.is_live():
        opt_key   = closed.get("option_key", "")
        qty       = closed.get("qty", 0)
        if opt_key and qty:
            order_service.schedule_exit(
                opt_key, qty, exit_price,
                sl_order_id=closed.get("sl_order_id", ""),
            )
    return closed


def partial_close_trade(exit_price: float, reason: str) -> dict | None:
    """
    Close half the lots at exit_price, keep rest open with SL moved to original target.
    Records a WIN trade for the closed portion.
    Returns {"closed_lots", "pnl_rs", "remaining_trade"} or None.
    """
    global _open_trade
    if not _open_trade or _open_trade.get("lots", 1) < 2:
        return None

    from uptox.services.capital_service import NIFTY_LOT_SIZE
    total_lots  = _open_trade["lots"]
    close_lots  = total_lots // 2
    remain_lots = total_lots - close_lots

    pnl_pts = round(exit_price - _open_trade["entry_price"], 1)
    pnl_rs  = round(pnl_pts * close_lots * NIFTY_LOT_SIZE, 0)

    # Record closed portion as a completed trade
    closed_rec = dict(_open_trade)
    closed_rec.update({
        "lots":        close_lots,
        "qty":         close_lots * NIFTY_LOT_SIZE,
        "invested":    round(close_lots * (_open_trade["invested"] / total_lots), 0),
        "exit_price":  round(exit_price, 1),
        "time_exit":   datetime.now(_IST).strftime("%H:%M:%S"),
        "pnl_pts":     pnl_pts,
        "pnl_rs":      pnl_rs,
        "outcome":     "WIN",
        "exit_reason": f"{reason}_partial",
        "peak_price":  round(exit_price, 1),
        "max_pnl_pts": pnl_pts,
        "max_pnl_rs":  pnl_rs,
    })
    _trades.append(closed_rec)

    # Update open trade: reduce lots, move SL to original target (lock profit floor)
    old_target = _open_trade["target"]
    _open_trade["lots"]           = remain_lots
    _open_trade["qty"]            = remain_lots * NIFTY_LOT_SIZE
    _open_trade["invested"]       = round(remain_lots * (_open_trade["invested"] / total_lots), 0)
    _open_trade["sl"]             = old_target          # SL now = original target
    _open_trade["target_crossed"] = False               # allow new target to form
    _open_trade["target"]         = round(exit_price + (exit_price - _open_trade["entry_price"]) * 0.5, 1)

    _save()
    _save_open(force=True)

    from uptox.services import capital_service
    capital_service.adjust_for_pnl(pnl_rs)

    log.info(
        "Partial CLOSE: %d lots @ %.1f  P&L=%.1fpts ₹%.0f  remain=%d lots  new_SL=%.1f",
        close_lots, exit_price, pnl_pts, pnl_rs, remain_lots, old_target,
    )
    _attach_costs(closed_rec["id"], _open_trade.get("option_key", ""),
                  close_lots * NIFTY_LOT_SIZE, closed_rec["entry_price"], exit_price)
    # Live: sell the banked half, then shrink the resting SL-M to the remaining
    # qty and lift its trigger to the new profit-floor SL (old target).
    from uptox.services import trading_mode, order_service
    if trading_mode.is_live():
        opt_key = _open_trade.get("option_key", "")
        if opt_key:
            order_service.schedule_order(opt_key, "SELL", close_lots * NIFTY_LOT_SIZE, price=exit_price)
        oid = _open_trade.get("sl_order_id", "")
        if oid:
            _open_trade["sl_order_trigger"] = old_target
            order_service.schedule_sl_modify(oid, _open_trade["qty"], old_target)
    return {"closed_lots": close_lots, "pnl_rs": pnl_rs, "remaining_trade": _open_trade}


def update_spot(nifty_ltp: float) -> None:
    """
    Tracks the Nifty 50 index's own move during the open trade — how many
    underlying points the market gave us, separate from option premium pts.
    spot_peak is the best Nifty level reached in the trade's favor (higher for
    CE, lower for PE), used at close to report "points left on the table" on
    the underlying itself, not just the option.
    """
    if not _open_trade or not nifty_ltp:
        return
    _open_trade["spot_current"] = round(nifty_ltp, 1)
    favorable_up = _open_trade["option"] == "CE"
    peak = _open_trade.get("spot_peak", nifty_ltp)
    if favorable_up:
        if nifty_ltp > peak:
            _open_trade["spot_peak"] = round(nifty_ltp, 1)
    else:
        if nifty_ltp < peak:
            _open_trade["spot_peak"] = round(nifty_ltp, 1)
    _save_open()


_PER_TRADE_LOSS_TRIGGERED = "per_trade_loss_limit"

def update_open_pnl(current_price: float) -> bool:
    """
    Update unrealized P&L. Returns True when target first crossed —
    caller should then call get_target_action() to decide close vs trail.
    Per-trade loss limit breach auto-closes via tick_handler checking get_per_trade_breach().
    """
    if not _open_trade or not current_price:
        return False
    from uptox.services.capital_service import NIFTY_LOT_SIZE
    _open_trade["current_price"] = current_price
    _open_trade["pnl_pts"] = round(current_price - _open_trade["entry_price"], 1)
    _open_trade["pnl_rs"]  = round(
        _open_trade["pnl_pts"] * _open_trade["lots"] * NIFTY_LOT_SIZE, 0
    )
    _save_open()
    if current_price >= _open_trade["target"] and not _open_trade.get("target_crossed"):
        _open_trade["target_crossed"] = True
        return True
    return False


def check_time_exit() -> str | None:
    """
    Time-based exits for option buyers (theta protection):
    - mae_exit_10min:   ≥10pt against entry within first 10 min — the direction
      read was wrong; exiting here saves ~5pt vs waiting for the full SL.
    - stale_exit_25min: >25 min in trade with pnl < +5pt — thesis failed, most
      losing option trades die slowly to theta rather than hitting SL fast.
      SKIPPED if the trade ever reached ≥+5pt: a real move already happened,
      so the trailing SL (with its profit lock) manages the position — the
      stale exit amputated a CE 24400 runner at +1.1pt on 2026-07-06 that
      later printed +22pt on the same contract.
    Returns exit reason string or None. Caller closes the trade.
    """
    if not _open_trade:
        return None
    entry_ts = _open_trade.get("entry_ts", 0.0)
    if not entry_ts:
        return None
    elapsed_min = (time.time() - entry_ts) / 60.0
    pnl = _open_trade.get("pnl_pts", 0.0)
    if elapsed_min <= 10.0 and pnl <= -10.0:
        return "mae_exit_10min"
    if elapsed_min >= 25.0 and pnl < 5.0:
        from uptox.services import trailing_sl_service
        ts = trailing_sl_service.get_status()
        peaked = ts.get("active") and ts.get("peak_pnl_pts", 0.0) >= 5.0
        if not peaked:
            return "stale_exit_25min"
    return None


def sync_sl_order() -> None:
    """
    Live mode: keep the single resting broker SL-M in step with the trail
    floor. Called per tick after trailing_sl_service.update(); only fires a
    modify when the floor rose ≥1pt past the last synced trigger — the broker
    order is MOVED, never duplicated (exchange allows one stop per position).
    """
    if not _open_trade:
        return
    oid = _open_trade.get("sl_order_id", "")
    if not oid:
        return
    from uptox.services import trading_mode
    if not trading_mode.is_live():
        return
    from uptox.services import trailing_sl_service
    st = trailing_sl_service.get_status()
    if not st.get("active"):
        return
    floor = st["sl_floor"]
    last  = _open_trade.get("sl_order_trigger", _open_trade.get("sl", 0.0))
    if floor - last >= 1.0:
        _open_trade["sl_order_trigger"] = floor
        from uptox.services import order_service
        order_service.schedule_sl_modify(oid, _open_trade["qty"], floor)


def get_per_trade_breach() -> bool:
    """True if open trade loss exceeds per-trade loss limit in ₹."""
    if not _open_trade:
        return False
    from uptox.services import capital_service
    limit = capital_service.get_per_trade_loss_rs()
    return _open_trade.get("pnl_rs", 0.0) < -limit


_VELOCITY_ATR_FRAC = 0.30   # spike when velocity ≥ this fraction of ATR per min
_VELOCITY_FLOOR    = 3.0    # pts/min — lower bound so tiny-ATR noise can't trigger
_HIGH_IV           = 20.0   # IV% above this = panic-priced options, take profits


def get_target_action(current_price: float) -> str:
    """
    Called the tick target is first crossed.
    Returns 'close' (volatile/spike market) or 'trail' (slow trend).

    Decision uses two signals:
      velocity = pts gained per minute since entry  (speed of the move)
      IV       = implied volatility of the option   (market uncertainty)

    Fast spike or high IV → close and bank profits.
    Slow trend and calm IV → trail SL past target, let it run.

    "Fast" is relative to the day's ATR — a 4.4pt/min move on an ATR-14
    afternoon is a spike, on an ATR-40 morning it's a drift. A fixed
    threshold (old: 15pt/min) never fired on quiet days and gave back
    spike profits to the snapback (2026-07-03 PE: peaked +19.9, kept +8.1).
    """
    if not _open_trade:
        return "close"

    # Range days mean-revert — extending the target past a consolidating-day
    # move gives the profit back to the snapback. Target hit = bank it.
    if str(_open_trade.get("regime", "")).startswith("CONSOLIDATING"):
        log.info("Target: CONSOLIDATING regime → close and bank (no extension)")
        return "close"

    elapsed_min = max((time.time() - _open_trade.get("entry_ts", time.time())) / 60, 0.5)
    velocity    = _open_trade["pnl_pts"] / elapsed_min   # pts/min

    from uptox.services import feed_store
    opt_tick = feed_store.get(_open_trade.get("option_key", ""))
    iv = opt_tick.iv if opt_tick else 0.0
    # Feed sends IV as a fraction (0.1 = 10%) but chain REST uses percent —
    # normalize to percent, else the iv >= 20 check can never fire.
    if 0 < iv < 1:
        iv *= 100.0

    atr = _open_trade.get("atr_at_entry", 0.0) or 30.0
    vel_thr  = max(_VELOCITY_FLOOR, atr * _VELOCITY_ATR_FRAC)
    volatile = velocity >= vel_thr or (iv > 0 and iv >= _HIGH_IV)

    # Store for UI display
    _open_trade["market_mode"]   = "VOLATILE" if volatile else "TRENDING"
    _open_trade["velocity_ppm"]  = round(velocity, 1)
    _open_trade["iv_at_target"]  = round(iv, 2)
    _open_trade["price_at_target"] = round(current_price, 1)

    if volatile:
        log.info(
            "Target: VOLATILE market (vel=%.1f pts/min, IV=%.1f) → close and bank profits",
            velocity, iv,
        )
        return "close"

    # Trending + multiple lots → partial exit (close half, trail rest)
    if _open_trade.get("lots", 1) > 1:
        log.info(
            "Target: TRENDING multi-lot (vel=%.1f pts/min, IV=%.1f) → partial exit",
            velocity, iv,
        )
        return "partial"

    log.info(
        "Target: TRENDING market (vel=%.1f pts/min, IV=%.1f) → trail past target",
        velocity, iv,
    )
    # Shift target forward by 1R so update_open_pnl doesn't re-trigger on the same price
    entry  = _open_trade.get("entry_price", 0.0)
    sl     = _open_trade.get("sl", 0.0)
    risk   = entry - sl if entry > sl else entry * 0.09
    new_tgt = round(current_price + risk * 2, 1)
    _open_trade["target"] = new_tgt
    from uptox.services import trailing_sl_service
    trailing_sl_service.shift_target(new_tgt)
    return "trail"


def has_open_trade() -> bool:
    return _open_trade is not None


def get_open_trade() -> dict | None:
    return _open_trade


def same_direction_streak(option: str) -> int:
    """
    Count consecutive most-recent CLOSED trades today in the same direction
    as `option`, walking backward until a different direction or no more
    trades today. 0 = fresh direction (no tightening needed).
    """
    today = datetime.now(_IST).strftime("%Y-%m-%d")
    today_trades = [t for t in _trades if t.get("date") == today]
    streak = 0
    for t in reversed(today_trades):
        if t.get("option") == option:
            streak += 1
        else:
            break
    return streak


_analysis_window: int = 100


def get_all_trades() -> list[dict]:
    return list(_trades)


def get_last_exit() -> dict | None:
    """Most recent closed trade — used for cooldown checks in dashboard."""
    return _trades[-1] if _trades else None


def get_analysis_trades() -> list[dict]:
    """Last _analysis_window closed trades used for performance/analysis panels."""
    return _trades[-_analysis_window:] if _trades else []


def set_analysis_window(n: int) -> None:
    global _analysis_window
    _analysis_window = max(5, int(n))
    log.info("Analysis window → last %d trades", _analysis_window)


def get_analysis_window() -> int:
    return _analysis_window


def get_stats(trades: list[dict] | None = None) -> dict:
    closed = trades if trades is not None else get_analysis_trades()
    if not closed:
        return {
            "total": 0, "wins": 0, "losses": 0, "win_rate": 0.0,
            "total_pnl_pts": 0.0, "total_pnl_rs": 0.0,
            "total_cost_rs": 0.0, "total_pnl_rs_net": 0.0,
            "avg_win": 0.0, "avg_loss": 0.0,
            "best": 0.0, "worst": 0.0, "expectancy": 0.0,
            "avg_rr": 0.0,
        }

    wins   = [t for t in closed if t["pnl_pts"] > 0]
    losses = [t for t in closed if t["pnl_pts"] <= 0]
    avg_w  = sum(t["pnl_pts"] for t in wins)   / len(wins)   if wins   else 0.0
    avg_l  = abs(sum(t["pnl_pts"] for t in losses) / len(losses)) if losses else 0.0
    wr     = len(wins) / len(closed)
    expect = wr * avg_w - (1 - wr) * avg_l

    # achieved RR = avg_win / avg_loss
    avg_rr = round(avg_w / avg_l, 2) if avg_l else 0.0

    total_pnl_rs = sum(t["pnl_rs"] for t in closed)
    total_cost   = sum(t.get("cost_rs", 0.0) or 0.0 for t in closed)

    return {
        "total":            len(closed),
        "wins":             len(wins),
        "losses":           len(losses),
        "win_rate":         round(wr * 100, 1),
        "total_pnl_pts":    round(sum(t["pnl_pts"] for t in closed), 1),
        "total_pnl_rs":     round(total_pnl_rs, 0),
        "total_cost_rs":    round(total_cost, 0),
        "total_pnl_rs_net": round(total_pnl_rs - total_cost, 0),
        "avg_win":          round(avg_w, 1),
        "avg_loss":         round(-avg_l, 1),    # negative = loss
        "best":             round(max((t["pnl_pts"] for t in closed), default=0.0), 1),
        "worst":            round(min((t["pnl_pts"] for t in closed), default=0.0), 1),
        "expectancy":       round(expect, 1),
        "avg_rr":           avg_rr,
    }


def get_daily_stats() -> dict:
    """
    Today's trading stats — used by daily-limit checks and Capital panel.
    Returns trades_today, pnl_rs_today, pnl_pct_today, consec_losses, blocked_reason.
    """
    from uptox.services import capital_service
    today  = datetime.now(_IST).strftime("%Y-%m-%d")
    today_trades = [t for t in _trades if t.get("date") == today]

    pnl_rs_total = sum(t.get("pnl_rs", 0.0) for t in today_trades)
    # Subtract baselines set at last Z-reset so limits restart from that point
    pnl_rs      = pnl_rs_total - _daily_pnl_reset_rs
    cap         = capital_service.get_capital()
    pnl_pct     = round(pnl_rs / cap * 100, 2) if cap else 0.0
    # Trade slots: only REAL trades consume one. Scratches (net P&L between
    # REAL_LOSS_PTS and REAL_WIN_PTS) are temporary stop-outs, not used
    # opportunities — on 2026-07-08 three scratch exits filled all 3 slots by
    # 13:39 and the system sat out the -430pt afternoon crash it had called.
    # Group by id first: partial exits write two records for one trade.
    _net_pts_by_id: dict = {}
    for i, t in enumerate(today_trades[_daily_trades_reset_n:]):
        k = t.get("id", i)
        _net_pts_by_id[k] = _net_pts_by_id.get(k, 0.0) + t.get("pnl_pts", 0.0)
    trade_count = sum(
        1 for p in _net_pts_by_id.values()
        if p <= REAL_LOSS_PTS or p >= REAL_WIN_PTS
    )

    # Consecutive REAL losses (from most recent backward, only trades after
    # the last Z-reset). Scratches neither count nor break the streak — a
    # -0.2pt trail exit says nothing about the directional read.
    consec_losses = 0
    for t in reversed(today_trades[_daily_trades_reset_n:]):
        pnl = t.get("pnl_pts", 0)
        if pnl <= REAL_LOSS_PTS:
            consec_losses += 1
        elif pnl > 0:
            break
        # scratch (REAL_LOSS_PTS < pnl <= 0): skip — neither counts nor breaks

    # Wins since last Z-reset — after 2 wins the day's edge is usually extracted;
    # trade 3+ tends to give profits back. Z-reset clears this along with count.
    # Only REAL wins count — with the profit lock turning scratches into
    # small wins, two +4pt exits must not trigger the 2-win "protect
    # profits" pause while the day's real move is still coming.
    wins_today = sum(1 for p in _net_pts_by_id.values() if p >= REAL_WIN_PTS)

    # Check limits — Z-reset only clears the loss-limit check; trade count/consec still enforced
    blocked_reason = None
    loss_limit_pct = capital_service.get_daily_loss_limit_pct()
    max_trades     = capital_service.get_max_trades_per_day()
    max_consec     = capital_service.get_max_consec_losses()

    global _slots_logged
    if trade_count != _slots_logged:
        log.info("Trade slots: %d/%d used today (%d remaining)",
                 trade_count, max_trades, max(0, max_trades - trade_count))
        _slots_logged = trade_count

    if pnl_pct <= -loss_limit_pct:
        blocked_reason = f"Daily loss limit hit ({pnl_pct:.1f}% / -{loss_limit_pct}%)"
    elif trade_count >= max_trades:
        blocked_reason = f"Max trades/day reached ({trade_count}/{max_trades})"
    elif consec_losses >= max_consec:
        blocked_reason = f"{consec_losses} consecutive losses — paused for day"
    elif wins_today >= 2 and pnl_rs > 0:
        blocked_reason = f"Win target hit ({wins_today} wins, ₹{pnl_rs:+.0f}) — protecting profits"

    return {
        "trades_today":    trade_count,
        "pnl_rs_today":    round(pnl_rs, 0),
        "pnl_pct_today":   pnl_pct,
        "consec_losses":   consec_losses,
        "wins_today":      wins_today,
        "blocked_reason":  blocked_reason,
        "can_trade":       blocked_reason is None,
        "block_overridden": _daily_pnl_reset_rs != 0.0,
    }


def reset_daily_block() -> None:
    """
    Full daily-block reset: fresh P&L budget, fresh trade slots, and a fresh
    consecutive-loss / win counter (all counters baseline from this moment).
    """
    global _daily_pnl_reset_rs, _daily_trades_reset_n
    from datetime import datetime as _dt
    today        = _dt.now(_IST).strftime("%Y-%m-%d")
    today_trades = [t for t in _trades if t.get("date") == today]
    _daily_pnl_reset_rs   = sum(t.get("pnl_rs", 0.0) for t in today_trades)
    _daily_trades_reset_n = len(today_trades)
    log.warning(
        "Daily block reset — P&L baseline ₹%.0f, trade/consec/win baselines at %d trades",
        _daily_pnl_reset_rs, _daily_trades_reset_n,
    )


def can_trade_today() -> tuple[bool, str | None]:
    """Returns (True, None) if allowed, (False, reason) if any daily limit hit."""
    ds = get_daily_stats()
    return ds["can_trade"], ds["blocked_reason"]


def entries_taken_today() -> set[tuple[str, str]]:
    """
    Set of (entry_type, option) already traded today, parsed from entry_reason.
    Lets once-per-day setups (orb, pdh_break, pdl_break) survive app restarts —
    the in-memory done-flags reset on restart, which allowed duplicate ORB/PDH
    trades after a mid-session deploy.
    """
    today = datetime.now(_IST).strftime("%Y-%m-%d")
    out: set[tuple[str, str]] = set()
    for t in _trades:
        if t.get("date") != today:
            continue
        et = (t.get("entry_reason") or "").split("|")[0].strip()
        if et:
            out.add((et, t.get("option", "")))
    return out


def direction_locked(option: str, spot_ltp: float = 0.0) -> tuple[bool, float]:
    """
    True if the last 2 closed trades today in this direction were both losses —
    that side isn't paying today; lock it for 60 min from the last exit.
    The 15-min cooldown alone just delays the same mistake (seen 2026-07-02:
    three consecutive CE losses, each entered right after cooldown expired).

    Resume override: the market moving ≥25pt beyond the last loss's entry spot
    in the trade's favor proves the losses were timing, not direction — stay
    locked and you sit out the exact move the trades predicted (2026-07-09:
    PE locked 14:15-15:15, the -100pt breakdown ran 15:00-15:15).
    Returns (locked, minutes_remaining).
    """
    today = datetime.now(_IST).strftime("%Y-%m-%d")
    same_dir = [t for t in _trades if t.get("date") == today and t.get("option") == option]
    # Walk backward: count REAL losses; winners break the streak; scratches
    # are invisible (neither count nor break) — same rule as consec_losses.
    real_losses = 0
    last_loss_ts = 0.0
    last_loss_spot = 0.0
    for t in reversed(same_dir):
        pnl = t.get("pnl_pts", 0)
        if pnl <= REAL_LOSS_PTS:
            real_losses += 1
            if not last_loss_ts:
                last_loss_ts = t.get("exit_ts") or 0.0
                last_loss_spot = t.get("spot_entry") or 0.0
            if real_losses >= 2:
                break
        elif pnl > 0:
            break
    if real_losses < 2 or not last_loss_ts:
        return False, 0.0
    mins_ago = (time.time() - last_loss_ts) / 60.0
    if mins_ago >= 60.0:
        return False, 0.0
    if spot_ltp and last_loss_spot:
        resumed = (
            (option == "CE" and spot_ltp > last_loss_spot + 25.0)
            or (option == "PE" and spot_ltp < last_loss_spot - 25.0)
        )
        if resumed:
            log.info(
                "Direction lock RELEASED: %s — spot %.1f moved ≥25pt beyond last "
                "loss entry %.1f, market proving the direction",
                option, spot_ltp, last_loss_spot,
            )
            return False, 0.0
    return True, 60.0 - mins_ago


# Load existing trades at import time
_load()
