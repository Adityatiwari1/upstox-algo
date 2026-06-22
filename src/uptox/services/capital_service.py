"""Capital management — position sizing, risk rules, daily limits."""
import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)

NIFTY_LOT_SIZE = 65   # NSE F&O standard lot

_CAPITAL_FILE = Path("logs/capital.json")

_base_capital: float  = 10_000.0
_capital: float       = 10_000.0
_risk_reward: float   = 2.0

# ── Daily limits (configurable) ───────────────────────────────────────────────
_daily_loss_limit_pct: float = 3.0    # % of capital → block new trades
_max_trades_per_day:   int   = 5      # max trades in a session
_max_consec_losses:    int   = 3      # consecutive losses before blocking


# ── Capital getters ───────────────────────────────────────────────────────────

def get_capital() -> float:       return _capital
def get_base_capital() -> float:  return _base_capital
def get_rr() -> float:            return _risk_reward
def get_daily_loss_limit_pct() -> float: return _daily_loss_limit_pct
def get_max_trades_per_day() -> int:     return _max_trades_per_day
def get_max_consec_losses() -> int:      return _max_consec_losses


def set_capital(c: float) -> None:
    global _base_capital, _capital
    delta         = max(1_000.0, float(c)) - _base_capital
    _base_capital = max(1_000.0, float(c))
    _capital      = round(_capital + delta, 2)
    _save_base()
    log.info("Base capital → ₹%s  (current ₹%s)", f"{_base_capital:,.0f}", f"{_capital:,.0f}")


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
    if _capital < 25_000:
        # Small account: deploy fully, 1 lot max
        lots       = 1
        capped_by  = "small_account_1lot"

    elif _capital < 1_00_000:
        # Mid account: max 5% capital at risk
        max_risk_rs = _capital * 0.05
        risk_lots   = int(max_risk_rs / sl_risk_per_lot) if sl_risk_per_lot > 0 else affordable
        lots        = min(affordable, max(1, risk_lots))
        capped_by   = "5pct_risk" if risk_lots < affordable else "affordability"

    else:
        # Large account: max 2% capital at risk, hard cap 10 lots
        max_risk_rs = _capital * 0.02
        risk_lots   = int(max_risk_rs / sl_risk_per_lot) if sl_risk_per_lot > 0 else affordable
        lots        = min(affordable, max(1, risk_lots), 10)
        capped_by   = "2pct_risk" if risk_lots < affordable else ("10lot_cap" if affordable > 10 else "affordability")

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


# ── SL / Target ───────────────────────────────────────────────────────────────

def levels(buy_price: float, delta: float | None) -> dict:
    if not buy_price:
        return {"sl": 0.0, "target": 0.0, "sl_pts": None, "tgt_pts": None}

    if delta and abs(delta) > 0.01:
        # ATR-based Nifty SL (1.5× ATR14 on 5min), fallback 30pt
        from uptox.services.atr_service import nifty_sl_points
        sl_nifty  = nifty_sl_points("NSE_INDEX|Nifty 50", multiplier=1.5, fallback=30.0)
        tgt_nifty = round(sl_nifty * _risk_reward, 1)
        sl        = round(max(0.5, buy_price - abs(delta) * sl_nifty), 1)
        target    = round(buy_price + abs(delta) * tgt_nifty, 1)
        return {"sl": sl, "target": target, "sl_pts": sl_nifty, "tgt_pts": tgt_nifty}

    sl     = round(max(0.5, buy_price * 0.70), 1)
    target = round(buy_price * (1.0 + 0.30 * _risk_reward), 1)
    return {"sl": sl, "target": target, "sl_pts": None, "tgt_pts": None}


# ── Load on import ────────────────────────────────────────────────────────────
_load_base()
