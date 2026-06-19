"""Capital management — position sizing, delta-based SL/Target, Risk:Reward."""
import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)

NIFTY_LOT_SIZE = 75   # NSE F&O standard lot

_CAPITAL_FILE = Path("logs/capital.json")

_base_capital: float  = 10_000.0   # what user set — never auto-modified
_capital: float       = 10_000.0   # base + realised P&L
_risk_reward: float   = 2.0


def get_capital() -> float:  return _capital
def get_base_capital() -> float: return _base_capital
def get_rr() -> float:       return _risk_reward


def set_capital(c: float) -> None:
    """User explicitly sets base capital. Recomputes running capital from current history."""
    global _base_capital, _capital
    _base_capital = max(1_000.0, float(c))
    _capital      = _base_capital
    _save_base()
    log.info("Base capital → ₹%s", f"{_base_capital:,.0f}")


def adjust_for_pnl(pnl_rs: float) -> None:
    """Add realised P&L to running capital after a trade closes."""
    global _capital
    _capital = round(_capital + pnl_rs, 2)
    log.info("Capital adjusted by ₹%.0f → ₹%s", pnl_rs, f"{_capital:,.0f}")


def sync_from_history(trades: list) -> None:
    """Recompute _capital = _base_capital + sum(pnl_rs) for all closed trades."""
    global _capital
    realised = sum(float(t.get("pnl_rs", 0.0)) for t in trades if t.get("outcome") != "OPEN")
    _capital  = round(_base_capital + realised, 2)
    log.info(
        "Capital synced from history: base=₹%.0f  realised=₹%.0f  current=₹%.0f",
        _base_capital, realised, _capital,
    )


def _save_base() -> None:
    try:
        _CAPITAL_FILE.parent.mkdir(exist_ok=True)
        _CAPITAL_FILE.write_text(
            json.dumps({"base": _base_capital}, ensure_ascii=False), encoding="utf-8"
        )
    except Exception as e:
        log.error("capital save error: %s", e)


def _load_base() -> None:
    global _base_capital, _capital
    if _CAPITAL_FILE.exists():
        try:
            data = json.loads(_CAPITAL_FILE.read_text(encoding="utf-8"))
            _base_capital = float(data.get("base", _base_capital))
            _capital      = _base_capital
        except Exception as e:
            log.error("capital load error: %s", e)


def set_rr(rr: float) -> None:
    global _risk_reward
    _risk_reward = round(max(1.0, min(5.0, float(rr))), 1)
    log.info("RR → 1:%.1f", _risk_reward)


def cycle_rr() -> float:
    """Cycle RR through 1.5 → 2.0 → 2.5 → 3.0 → 1.5."""
    steps = [1.5, 2.0, 2.5, 3.0]
    idx   = steps.index(_risk_reward) if _risk_reward in steps else 0
    set_rr(steps[(idx + 1) % len(steps)])
    return _risk_reward


def suggest_rr(trend_score: int) -> float:
    """System-suggested RR based on signal strength."""
    s = abs(trend_score)
    if s >= 5: return 3.0
    if s >= 4: return 2.5
    if s >= 3: return 2.0
    return 1.5


def sizing(buy_price: float) -> dict:
    """Lots affordable given capital and buy_price per unit. lots=0 means can't afford."""
    if not buy_price:
        return {"lots": 0, "lot_cost": 0.0, "invested": 0.0}
    lot_cost = round(buy_price * NIFTY_LOT_SIZE, 2)
    lots     = int(_capital / lot_cost)   # 0 when lot_cost > capital
    return {"lots": lots, "lot_cost": lot_cost, "invested": round(lots * lot_cost, 2)}


def levels(buy_price: float, delta: float | None) -> dict:
    """
    Delta-based SL/Target when delta known (30-pt Nifty SL).
    Falls back to percentage-based when delta is missing.
    """
    if not buy_price:
        return {"sl": 0.0, "target": 0.0, "sl_pts": None, "tgt_pts": None}

    if delta and abs(delta) > 0.01:
        sl_nifty  = 30
        tgt_nifty = int(sl_nifty * _risk_reward)
        sl        = round(max(0.5, buy_price - abs(delta) * sl_nifty), 1)
        target    = round(buy_price + abs(delta) * tgt_nifty, 1)
        return {"sl": sl, "target": target, "sl_pts": sl_nifty, "tgt_pts": tgt_nifty}

    sl     = round(max(0.5, buy_price * 0.70), 1)
    target = round(buy_price * (1.0 + 0.30 * _risk_reward), 1)
    return {"sl": sl, "target": target, "sl_pts": None, "tgt_pts": None}


# Load persisted base on import
_load_base()
