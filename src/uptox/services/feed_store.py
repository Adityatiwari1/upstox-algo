"""
In-memory store for latest tick data.
Feed client writes here; UI widgets read from here.
"""
from dataclasses import dataclass, field


@dataclass
class TickData:
    instrument_key: str
    ltp: float = 0.0
    ltt: int = 0
    ltq: int = 0
    close: float = 0.0
    # depth
    bids: list[dict] = field(default_factory=list)
    asks: list[dict] = field(default_factory=list)
    # greeks
    delta: float = 0.0
    gamma: float = 0.0
    theta: float = 0.0
    vega: float = 0.0
    iv: float = 0.0
    # extended
    oi: float = 0.0
    change_oi: float = 0.0
    tbq: float = 0.0
    tsq: float = 0.0
    mbp_buy: int = 0
    mbp_sell: int = 0
    vtt: int = 0
    atp: float = 0.0


# Global store
_store: dict[str, TickData] = {}
_cum_delta: dict[str, float] = {}   # session cumulative delta per instrument
_feed_status: dict[str, str] = {"status": "disconnected"}


def flow_qty(tick: TickData | None) -> tuple[float, float]:
    """
    (buy, sell) quantity for order-flow scoring.
    Uses Upstox tbq/tsq when present. Upstox often sends tbq=tsq=0 for index
    options (known feed limitation) — falls back to live depth (bid/ask qty sum).
    """
    if not tick:
        return 0.0, 0.0
    if tick.tbq or tick.tsq:
        return tick.tbq, tick.tsq
    buy  = sum(float(l.get("qty", 0)) for l in tick.bids) if tick.bids else 0.0
    sell = sum(float(l.get("qty", 0)) for l in tick.asks) if tick.asks else 0.0
    return buy, sell


def update(instrument_key: str, tick: TickData) -> None:
    prev = _store.get(instrument_key)
    _store[instrument_key] = tick
    if instrument_key not in _cum_delta:
        _cum_delta[instrument_key] = 0.0
    # cum_delta = executed flow accumulation via tick rule (ltq × price direction).
    # tbq/tsq are order queue snapshots — used for delta_sc, not here.
    if prev and tick.ltq > 0:
        direction = 1 if tick.ltp > prev.ltp else -1 if tick.ltp < prev.ltp else 0
        _cum_delta[instrument_key] += tick.ltq * direction


def get(instrument_key: str) -> TickData | None:
    return _store.get(instrument_key)


def cum_delta(instrument_key: str) -> float:
    return _cum_delta.get(instrument_key, 0.0)


def set_status(status: str) -> None:
    _feed_status["status"] = status


def get_status() -> str:
    return _feed_status["status"]


def all_ticks() -> dict[str, TickData]:
    return dict(_store)
