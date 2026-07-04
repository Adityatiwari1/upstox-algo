"""
Global pause switch for auto-entry — set via Telegram /pause and /resume.
Checked by dashboard.py's entry gate; does not affect an already-open trade
(trailing SL / exits keep running regardless).
"""
from __future__ import annotations

_paused: bool = False


def is_paused() -> bool:
    return _paused


def pause() -> None:
    global _paused
    _paused = True


def resume() -> None:
    global _paused
    _paused = False
