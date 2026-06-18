"""Capital management — position sizing, delta-based SL/Target, Risk:Reward."""
import logging

log = logging.getLogger(__name__)

NIFTY_LOT_SIZE = 75   # NSE F&O standard lot

_capital: float    = 10_000.0
_risk_reward: float = 2.0        # default 1:2


def get_capital() -> float:  return _capital
def get_rr() -> float:       return _risk_reward


def set_capital(c: float) -> None:
    global _capital
    _capital = max(1_000.0, float(c))
    log.info("Capital → ₹%s", f"{_capital:,.0f}")


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
