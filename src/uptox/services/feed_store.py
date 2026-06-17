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


def update(instrument_key: str, tick: TickData) -> None:
    prev = _store.get(instrument_key)
    _store[instrument_key] = tick
    # cumulative delta: session running total of (tbq - tsq) delta
    tick_delta = tick.tbq - tick.tsq
    if instrument_key not in _cum_delta:
        _cum_delta[instrument_key] = 0.0
    if prev and (tick.tbq != prev.tbq or tick.tsq != prev.tsq):
        _cum_delta[instrument_key] += tick_delta - (prev.tbq - prev.tsq)
    elif not prev:
        _cum_delta[instrument_key] = tick_delta


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
