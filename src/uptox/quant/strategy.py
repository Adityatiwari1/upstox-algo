"""Strategy interface shared by every quant model. A Strategy consumes a
rolling history of market-snapshot dicts (same shape as one row of
logs/market_data/*.jsonl) and emits a Signal or nothing — it never touches
sizing, exits, or order placement (that's QuantEngine's job)."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Literal


@dataclass
class Signal:
    option: Literal["CE", "PE"]
    reason: str
    score: float
    strategy_id: str


class Strategy(ABC):
    strategy_id: str

    @abstractmethod
    def generate_signal(self, history: list[dict]) -> Signal | None:
        ...
