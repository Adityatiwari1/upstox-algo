"""Paper/live flag for one quant strategy — independent of the global
trading_mode and of every other strategy's flag. order_service has no
internal paper/live gate of its own (verified against order_service.py),
so this check is the only thing standing between a quant signal and a
real order. Reusing the global flag would mean flipping focus-mode live
silently arms an unproven quant model too — hence one instance per
strategy_id, not a module singleton."""
from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)

_VALID = ("paper", "live")


class QuantTradingMode:

    def __init__(self, strategy_id: str):
        self.strategy_id = strategy_id
        self._file = Path(f"logs/quant_mode_{strategy_id}.json")
        self._mode = "paper"
        self._load()

    def get_mode(self) -> str:
        return self._mode

    def is_live(self) -> bool:
        return self._mode == "live"

    def is_paper(self) -> bool:
        return self._mode == "paper"

    def set_mode(self, mode: str) -> None:
        if mode not in _VALID:
            raise ValueError(f"Invalid mode '{mode}'. Must be one of {_VALID}")
        self._mode = mode
        self._save()
        log.info("Quant mode [%s] -> %s", self.strategy_id, mode.upper())

    def _save(self) -> None:
        try:
            self._file.parent.mkdir(parents=True, exist_ok=True)
            self._file.write_text(json.dumps({"mode": self._mode}), encoding="utf-8")
        except OSError as e:
            log.error("quant_trading_mode save error [%s]: %s", self.strategy_id, e)

    def _load(self) -> None:
        if self._file.exists():
            try:
                data = json.loads(self._file.read_text(encoding="utf-8"))
                m = data.get("mode", "paper")
                if m in _VALID:
                    self._mode = m
            except (OSError, json.JSONDecodeError) as e:
                log.error("quant_trading_mode load error [%s]: %s", self.strategy_id, e)
