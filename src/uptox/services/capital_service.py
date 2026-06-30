"""Capital management — position sizing, risk rules, daily limits."""
import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)

NIFTY_LOT_SIZE = 65   # NSE F&O lot size

_CAPITAL_FILE = Path("logs/capital.json")

_base_capital: float  = 10_000.0
_capital: float       = 10_000.0
_risk_reward: float   = 2.0

# ── Daily limits (configurable) ───────────────────────────────────────────────
_daily_loss_limit_pct: float = 5.0    # % of capital → block new trades
_max_trades_per_day:   int   = 3      # max trades in a session
_max_consec_losses:    int   = 2      # consecutive losses before blocking
# per_trade_loss_rs is DERIVED — not stored: daily_limit_rs / max_trades


# ── Capital getters ───────────────────────────────────────────────────────────

def get_capital() -> float:       return _capital
def get_base_capital() -> float:  return _base_capital
def get_rr() -> float:            return _risk_reward
def get_daily_loss_limit_pct() -> float: return _daily_loss_limit_pct
def get_max_trades_per_day() -> int:     return _max_trades_per_day
def get_max_consec_losses() -> int:      return _max_consec_losses

def get_daily_loss_limit_rs() -> float:
    """Daily max loss in ₹ = capital × daily_loss_pct%."""
    return round(_capital * _daily_loss_limit_pct / 100, 2)

def get_per_trade_loss_rs() -> float:
    """Per-trade loss limit in ₹ = daily_limit_rs / max_trades."""
    return round(get_daily_loss_limit_rs() / max(_max_trades_per_day, 1), 2)


def set_capital(c: float) -> None:
    """Set the BASE (starting) capital, used only for lifetime % tracking. Leaves current capital untouched."""
    global _base_capital
    _base_capital = max(1_000.0, float(c))
    _save_base()
    log.info("Base capital → ₹%s", f"{_base_capital:,.0f}")


def set_current_capital(c: float) -> None:
    """Directly set current capital (e.g. to match your real broker balance)."""
    global _capital
    _capital = max(1_000.0, round(float(c), 2))
    log.info("Current capital → ₹%s", f"{_capital:,.0f}")


def adjust_for_pnl(pnl_rs: float) -> None:
    global _capital
    _capital = round(_capital + pnl_rs, 2)
    log.info("Capital adjusted by ₹%.0f → ₹%s", pnl_rs, f"{_capital:,.0f}")


def sync_from_history(trades: list) -> None:
    global _capital
    realised = sum(float(t.get("pnl_rs", 0.0)) for t in trades if t.get("outcome") != "OPEN")
    _capital  = round(_base_capital + realised, 2)
    log.info(
        "Capital synced: base=₹%.0f  realised=₹%.0f  current=₹%.0f",
        _base_capital, realised, _capital,
    )


def set_daily_loss_limit_pct(pct: float) -> None:
    global _daily_loss_limit_pct
    _daily_loss_limit_pct = max(0.5, min(10.0, float(pct)))
    log.info("Daily loss limit → %.1f%% of capital", _daily_loss_limit_pct)


def set_max_trades_per_day(n: int) -> None:
    global _max_trades_per_day
    _max_trades_per_day = max(1, int(n))


def set_max_consec_losses(n: int) -> None:
    global _max_consec_losses
    _max_consec_losses = max(1, int(n))




# ── Persistence ───────────────────────────────────────────────────────────────

def _save_base() -> None:
    try:
        _CAPITAL_FILE.parent.mkdir(exist_ok=True)
        _CAPITAL_FILE.write_text(
            json.dumps({
                "base":               _base_capital,
                "daily_loss_pct":     _daily_loss_limit_pct,
                "max_trades_per_day": _max_trades_per_day,
                "max_consec_losses":  _max_consec_losses,
            }, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception as e:
        log.error("capital save error: %s", e)


def _load_base() -> None:
    global _base_capital, _capital, _daily_loss_limit_pct
    global _max_trades_per_day, _max_consec_losses
    if _CAPITAL_FILE.exists():
        try:
            data = json.loads(_CAPITAL_FILE.read_text(encoding="utf-8"))
            _base_capital         = float(data.get("base", _base_capital))
            _capital              = _base_capital
            _daily_loss_limit_pct = float(data.get("daily_loss_pct", _daily_loss_limit_pct))
            _max_trades_per_day   = int(data.get("max_trades_per_day", _max_trades_per_day))
            _max_consec_losses    = int(data.get("max_consec_losses", _max_consec_losses))
        except Exception as e:
            log.error("capital load error: %s", e)


# ── RR ────────────────────────────────────────────────────────────────────────

def set_rr(rr: float) -> None:
    global _risk_reward
    _risk_reward = round(max(1.0, min(5.0, float(rr))), 1)
    log.info("RR → 1:%.1f", _risk_reward)


def cycle_rr() -> float:
    steps = [1.5, 2.0, 2.5, 3.0]
    idx   = steps.index(_risk_reward) if _risk_reward in steps else 0
    set_rr(steps[(idx + 1) % len(steps)])
    return _risk_reward


def suggest_rr(trend_score: int) -> float:
    s = abs(trend_score)
    if s >= 5: return 3.0
    if s >= 4: return 2.5
    if s >= 3: return 2.0
    return 1.5


# ── Sizing ────────────────────────────────────────────────────────────────────

def sizing(buy_price: float) -> dict:
    """
    Simple affordability check (used by _affordable_strike for OTM search).
    Returns lots=0 if can't afford even 1 lot.
    """
    if not buy_price:
        return {"lots": 0, "lot_cost": 0.0, "invested": 0.0}
    lot_cost = round(buy_price * NIFTY_LOT_SIZE, 2)
    lots     = int(_capital / lot_cost)
    return {"lots": lots, "lot_cost": lot_cost, "invested": round(lots * lot_cost, 2)}


def sizing_with_risk(buy_price: float, sl_price: float) -> dict:
    """
    Risk-adjusted lot sizing based on capital tier.

    Tiers:
      < ₹25K  : max 1 lot, use up to 100% capital (small account)
      ₹25K–₹1L: max risk 5% of capital per trade
      > ₹1L   : max risk 2% of capital per trade, hard cap 10 lots

    Returns: lots, lot_cost, invested, risk_rs, risk_pct, capped_by
    """
    if not buy_price or buy_price <= 0:
        return _zero_sizing()

    lot_cost      = round(buy_price * NIFTY_LOT_SIZE, 2)
    affordable    = int(_capital / lot_cost)   # hard affordability ceiling

    if affordable == 0:
        return _zero_sizing()

    sl_risk_per_lot = round((buy_price - sl_price) * NIFTY_LOT_SIZE, 2) if sl_price and sl_price < buy_price else lot_cost * 0.30

    # ── Tier rules ────────────────────────────────────────────────────────
    # Lot sizing from per-trade loss limit (derived from daily limit / max_trades)
    max_risk_rs = get_per_trade_loss_rs()
    risk_lots   = int(max_risk_rs / sl_risk_per_lot) if sl_risk_per_lot > 0 else affordable

    if _capital < 25_000:
        lots      = min(affordable, max(1, risk_lots), 1)
        capped_by = "small_account_1lot"
    elif _capital < 1_00_000:
        lots      = min(affordable, max(1, risk_lots))
        capped_by = "per_trade_limit" if risk_lots < affordable else "affordability"
    else:
        lots      = min(affordable, max(1, risk_lots), 10)
        capped_by = "per_trade_limit" if risk_lots < affordable else ("10lot_cap" if affordable > 10 else "affordability")

    invested  = round(lots * lot_cost, 2)
    risk_rs   = round(lots * sl_risk_per_lot, 2)
    risk_pct  = round(risk_rs / _capital * 100, 2) if _capital else 0.0

    return {
        "lots":       lots,
        "lot_cost":   lot_cost,
        "invested":   invested,
        "risk_rs":    risk_rs,
        "risk_pct":   risk_pct,
        "capped_by":  capped_by,
    }


def get_risk_info(buy_price: float, sl_price: float, lots: int) -> dict:
    """Risk metrics for a given position (used for display)."""
    if not buy_price or not lots:
        return {"risk_rs": 0.0, "risk_pct": 0.0, "reward_rs": 0.0, "rr": 0.0}
    sl   = sl_price or buy_price * 0.80
    tgt  = buy_price * (1 + (buy_price - sl) / buy_price * _risk_reward)
    risk_rs   = round((buy_price - sl) * lots * NIFTY_LOT_SIZE, 0)
    reward_rs = round((tgt - buy_price) * lots * NIFTY_LOT_SIZE, 0)
    rr        = round(reward_rs / risk_rs, 2) if risk_rs else 0.0
    return {
        "risk_rs":   risk_rs,
        "risk_pct":  round(risk_rs / _capital * 100, 2) if _capital else 0.0,
        "reward_rs": reward_rs,
        "rr":        rr,
    }


def _zero_sizing() -> dict:
    return {"lots": 0, "lot_cost": 0.0, "invested": 0.0,
            "risk_rs": 0.0, "risk_pct": 0.0, "capped_by": "unaffordable"}


# ── Expiry afternoon detection ────────────────────────────────────────────────

def is_expiry_afternoon() -> bool:
    """True if today is expiry day AND time is after 13:00 IST."""
    from datetime import datetime, time as dt_time
    from zoneinfo import ZoneInfo
    try:
        from uptox.services import option_chain_service
        expiry = option_chain_service.get_expiry()
        if not expiry:
            return False
        now = datetime.now(ZoneInfo("Asia/Kolkata"))
        return now.strftime("%Y-%m-%d") == expiry and now.time() >= dt_time(13, 0)
    except Exception:
        return False


def is_expiry_day() -> bool:
    """True any time on expiry date (regardless of hour)."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    try:
        from uptox.services import option_chain_service
        expiry = option_chain_service.get_expiry()
        if not expiry:
            return False
        return datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%Y-%m-%d") == expiry
    except Exception:
        return False


# ── SL / Target ───────────────────────────────────────────────────────────────

def levels(buy_price: float, delta: float | None = None, iv: float = 0.0, repeat_count: int = 0) -> dict:  # noqa: ARG001
    """
    repeat_count: how many consecutive same-direction trades already happened
    today before this one (0 = first/fresh direction). Each repeat at the same
    level carries more reversal risk, so SL tightens the more times in a row
    we've tried this direction — regardless of which entry rule fired it.
    """
    if not buy_price:
        return {"sl": 0.0, "target": 0.0, "sl_pts": None, "tgt_pts": None, "regime": "UNKNOWN"}

    from uptox.services.atr_service import get_regime, min_sl_pts
    r        = get_regime(iv=iv)
    sl_pct   = r["sl_pct"]
    rr       = r["rr"]

    # Expiry day after 13:00 IST → tight SL only; target stays regime-based so big spikes run
    if is_expiry_afternoon():
        sl_pct = 0.05
        r      = {**r, "regime": f"{r['regime']}+EXPIRY"}

    # Repeat same-direction trade today → higher chance of chop/reversal at this
    # level, so cut risk further each time instead of giving it fresh-signal room.
    tighten_factor = max(0.40, 1.0 - 0.15 * repeat_count) if repeat_count > 0 else 1.0
    if repeat_count > 0:
        sl_pct = round(sl_pct * tighten_factor, 4)
        r      = {**r, "regime": f"{r['regime']}+REPEAT{repeat_count}"}

    pct_sl   = round(buy_price * sl_pct, 1)
    abs_sl   = min_sl_pts(buy_price)          # tiered floor — prevents noise hits
    if repeat_count > 0:
        abs_sl = round(abs_sl * tighten_factor, 1)  # tighten the floor too, or it dominates pct_sl
    sl_pts   = max(pct_sl, abs_sl)            # always at least the tiered minimum
    sl       = round(max(0.5, buy_price - sl_pts), 1)
    tgt_pts  = round(sl_pts * rr, 1)
    target   = round(buy_price + tgt_pts, 1)
    return {
        "sl": sl, "target": target,
        "sl_pts": sl_pts, "tgt_pts": tgt_pts,
        "regime": r["regime"], "atr": r["atr"],
    }


# ── Expiry sizing ─────────────────────────────────────────────────────────────

_EXPIRY_ALLOC_PCT: float = 0.10   # max 10% of capital as premium (full-loss scenario)
_EXPIRY_MAX_LOTS:  int   = 20


def sizing_expiry(buy_price: float) -> dict:
    """
    Expiry afternoon sizing — binary bet, not risk-per-SL.
    Allocate up to 10% of capital as max premium (worst case = full loss of premium).
    """
    if not buy_price or buy_price <= 0:
        return _zero_sizing()
    lot_cost   = round(buy_price * NIFTY_LOT_SIZE, 2)
    affordable = max(1, int(_capital / lot_cost))
    max_alloc  = _capital * _EXPIRY_ALLOC_PCT
    alloc_lots = max(1, int(max_alloc / lot_cost))
    lots       = min(affordable, alloc_lots, _EXPIRY_MAX_LOTS)
    invested   = round(lots * lot_cost, 2)
    return {
        "lots":      lots,
        "lot_cost":  lot_cost,
        "invested":  invested,
        "risk_rs":   invested,        # worst case = full premium
        "risk_pct":  round(invested / _capital * 100, 2) if _capital else 0.0,
        "capped_by": "expiry_10pct",
    }


# ── Load on import ────────────────────────────────────────────────────────────
_load_base()
