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
_HZ_START         = dt_time(15, 30)   # Hero/Zero window: last 15 mins before 3:45 close
_HZ_FORCE_CLOSE   = dt_time(15, 44)   # force exit 1 min before 3:45 PM settlement
_SESSION_CLOSE    = dt_time(15, 25)   # force-exit ALL trades at 3:25 PM IST

log = logging.getLogger(__name__)

_TRADES_FILE     = Path("logs/trades.json")
_TRADES_XLSX     = Path("logs/trades.xlsx")
_OPEN_TRADE_FILE = Path("logs/open_trade.json")   # persists open trade across restarts
_trades: list[dict] = []
_open_trade: dict | None = None

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
    ("Hero/Zero",    "hero_zero"),
]


# ── persistence ───────────────────────────────────────────────────────────────

def _save_open() -> None:
    """Persist open trade to disk so restarts can recover it."""
    try:
        _OPEN_TRADE_FILE.parent.mkdir(exist_ok=True)
        if _open_trade:
            _OPEN_TRADE_FILE.write_text(
                json.dumps(_open_trade, indent=2, ensure_ascii=False), encoding="utf-8"
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
            data = json.loads(_OPEN_TRADE_FILE.read_text(encoding="utf-8"))
            if data.get("outcome") == "OPEN":
                _open_trade = data
                ep      = float(data.get("entry_price", 0))
                sl      = float(data.get("sl", 0))
                opt     = data.get("option", "")
                opt_key = data.get("option_key", "")
                hz      = bool(data.get("hero_zero", False))
                if ep:
                    from uptox.services import trailing_sl_service
                    if hz:
                        trailing_sl_service.activate_hero_zero(ep, opt, opt_key)
                    else:
                        trail_pct = (ep - sl) / ep if (sl and sl < ep) else 0.20
                        trailing_sl_service.set_entry(ep, opt, opt_key, trail_pct=trail_pct)
                log.info(
                    "Restored open trade from disk: %s %s @ %.1f  SL=%.1f",
                    opt, data.get("strike"), ep, sl,
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


# ── Hero/Zero detection ───────────────────────────────────────────────────────

def is_hero_zero_window(expiry: str) -> bool:
    """True if current time is in the last 15 mins of the expiry date."""
    now = datetime.now(_IST)
    return now.strftime("%Y-%m-%d") == expiry and now.time() >= _HZ_START


def should_force_close() -> bool:
    """True at 3:44 PM on expiry day — force exit Hero/Zero before settlement."""
    ot = _open_trade
    if not ot or not ot.get("hero_zero"):
        return False
    now = datetime.now(_IST)
    return (now.strftime("%Y-%m-%d") == ot.get("expiry", "")
            and now.time() >= _HZ_FORCE_CLOSE)


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
) -> None:
    global _open_trade
    if _open_trade:
        return  # already in a trade
    _open_trade = {
        "id":          str(uuid.uuid4())[:8],
        "date":        datetime.now().strftime("%Y-%m-%d"),
        "time_entry":  datetime.now().strftime("%H:%M:%S"),
        "entry_ts":    time.time(),
        "time_exit":   None,
        "strike":      strike,
        "option":      option,
        "expiry":      expiry,
        "option_key":  option_key,
        "entry_price": round(entry_price, 1),
        "exit_price":  None,
        "lots":        lots,
        "invested":    round(invested, 0),
        "sl":          round(sl, 1),
        "target":      round(target, 1),
        "delta":       round(delta, 3) if delta else None,
        "spot_entry":  round(spot, 1),
        "current_price": entry_price,
        "pnl_pts":     0.0,
        "pnl_rs":      0.0,
        "outcome":      "OPEN",
        "exit_reason":  None,
        "entry_reason": entry_reason,
        "score":        score,
        "sma_score":    sma_score,
        "flow_score":   flow_score,
    }
    # Hero/Zero: last 15 mins of expiry → step-SL mode, 50% initial risk
    hz = is_hero_zero_window(expiry)
    _open_trade["hero_zero"] = hz   # type: ignore[index]

    from uptox.services import trailing_sl_service
    if hz:
        trailing_sl_service.activate_hero_zero(entry_price, option, option_key)
        log.info(
            "Paper OPEN [HERO/ZERO]: %s %s @ %.1f  SL=%.1f (50%%)  lots=%d",
            option, strike, entry_price, round(entry_price * 0.50, 1), lots,
        )
    else:
        trail_pct = (entry_price - sl) / entry_price if (sl and sl < entry_price) else 0.20
        trailing_sl_service.set_entry(entry_price, option, option_key, trail_pct=trail_pct)
        log.info(
            "Paper OPEN: %s %s @ %.1f  SL=%.1f  TGT=%.1f  trail_pct=%.0f%%  lots=%d",
            option, strike, entry_price, sl, target, trail_pct * 100, lots,
        )
    _save_open()


def close_trade(exit_price: float, reason: str) -> dict | None:
    global _open_trade
    if not _open_trade:
        return None
    from uptox.services.capital_service import NIFTY_LOT_SIZE

    pnl_pts = round(exit_price - _open_trade["entry_price"], 1)
    pnl_rs  = round(pnl_pts * _open_trade["lots"] * NIFTY_LOT_SIZE, 0)

    # Outcome based purely on exit price — reason is just metadata
    if exit_price >= _open_trade["target"]:
        outcome = "TARGET_HIT"     # closed above target (trail ran past it)
    elif exit_price <= _open_trade["sl"]:
        outcome = "SL_HIT"         # closed at or below initial SL
    elif pnl_pts > 0:
        outcome = "WIN"            # trail SL caught profits between SL and target
    elif pnl_pts < 0:
        outcome = "LOSS"
    else:
        outcome = "BREAKEVEN"

    _open_trade.update({
        "exit_price":  round(exit_price, 1),
        "time_exit":   datetime.now().strftime("%H:%M:%S"),
        "pnl_pts":     pnl_pts,
        "pnl_rs":      pnl_rs,
        "outcome":     outcome,
        "exit_reason": reason,
    })
    closed = dict(_open_trade)
    closed.pop("current_price", None)
    _trades.append(closed)
    _open_trade = None
    _save()
    _save_open()   # clears open_trade.json — trade is closed
    # Adjust running capital with realised P&L
    from uptox.services import capital_service, trailing_sl_service
    capital_service.adjust_for_pnl(pnl_rs)
    trailing_sl_service.exit_trade()

    log.info(
        "Paper CLOSE: %s  P&L=%.1fpts  ₹%.0f  reason=%s",
        outcome, pnl_pts, pnl_rs, reason,
    )
    return closed


def update_open_pnl(current_price: float) -> bool:
    """
    Update unrealized P&L. Returns True the tick target is first crossed —
    caller should then call get_target_action() to decide close vs trail.
    """
    if not _open_trade or not current_price:
        return False
    from uptox.services.capital_service import NIFTY_LOT_SIZE
    _open_trade["current_price"] = current_price
    _open_trade["pnl_pts"] = round(current_price - _open_trade["entry_price"], 1)
    _open_trade["pnl_rs"]  = round(
        _open_trade["pnl_pts"] * _open_trade["lots"] * NIFTY_LOT_SIZE, 0
    )
    if current_price >= _open_trade["target"] and not _open_trade.get("target_crossed"):
        _open_trade["target_crossed"] = True
        return True  # signal to caller: target just crossed this tick
    return False


_HIGH_VELOCITY = 15.0   # pts/min — faster than this = spike, take profits
_HIGH_IV       = 20.0   # IV above this = volatile market, take profits


def get_target_action(current_price: float) -> str:
    """
    Called the tick target is first crossed.
    Returns 'close' (volatile/spike market) or 'trail' (slow trend).

    Decision uses two signals:
      velocity = pts gained per minute since entry  (speed of the move)
      IV       = implied volatility of the option   (market uncertainty)

    Fast spike or high IV → close and bank profits.
    Slow trend and calm IV → trail SL past target, let it run.
    """
    if not _open_trade:
        return "close"

    # Hero/Zero: never close at target — let step-SL handle it
    if _open_trade.get("hero_zero"):
        log.info("Target crossed in Hero/Zero mode — step-SL continues, trade stays open")
        _open_trade["market_mode"] = "HERO_ZERO"
        return "trail"

    elapsed_min = max((time.time() - _open_trade.get("entry_ts", time.time())) / 60, 0.5)
    velocity    = _open_trade["pnl_pts"] / elapsed_min   # pts/min

    from uptox.services import feed_store
    opt_tick = feed_store.get(_open_trade.get("option_key", ""))
    iv = opt_tick.iv if opt_tick else 0.0

    volatile = velocity >= _HIGH_VELOCITY or (iv > 0 and iv >= _HIGH_IV)

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

    log.info(
        "Target: TRENDING market (vel=%.1f pts/min, IV=%.1f) → trail past target",
        velocity, iv,
    )
    return "trail"


def check_exit(current_price: float) -> str | None:
    """
    Returns exit reason if SL or target is hit, else None.
    Called per-tick in tick_handler.
    """
    if not _open_trade or not current_price:
        return None
    if current_price <= _open_trade["sl"]:
        return "sl_hit"
    if current_price >= _open_trade["target"]:
        return "target_hit"
    return None


def has_open_trade() -> bool:
    return _open_trade is not None


def get_open_trade() -> dict | None:
    return _open_trade


_analysis_window: int = 100


def get_all_trades() -> list[dict]:
    return list(_trades)


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

    return {
        "total":         len(closed),
        "wins":          len(wins),
        "losses":        len(losses),
        "win_rate":      round(wr * 100, 1),
        "total_pnl_pts": round(sum(t["pnl_pts"] for t in closed), 1),
        "total_pnl_rs":  round(sum(t["pnl_rs"]  for t in closed), 0),
        "avg_win":       round(avg_w, 1),
        "avg_loss":      round(-avg_l, 1),    # negative = loss
        "best":          round(max((t["pnl_pts"] for t in closed), default=0.0), 1),
        "worst":         round(min((t["pnl_pts"] for t in closed), default=0.0), 1),
        "expectancy":    round(expect, 1),
        "avg_rr":        avg_rr,
    }


# Load existing trades at import time
_load()
