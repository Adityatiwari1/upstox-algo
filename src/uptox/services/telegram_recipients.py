"""
Extra Telegram chat IDs, in addition to the primary one in .env.
Lets you fan a notification out to several chats (e.g. yourself + a group)
without touching env vars. Persisted to logs/telegram_recipients.json.
"""
from __future__ import annotations
import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)

_FILE = Path("logs/telegram_recipients.json")
_extra_ids: list[str] = []


def get_extra() -> list[str]:
    return list(_extra_ids)


def get_all() -> list[str]:
    """Primary chat_id (from settings) + extras, deduped, non-empty."""
    from uptox.config.settings import settings
    primary = getattr(settings, "telegram_chat_id", "").strip()
    ids = ([primary] if primary else []) + _extra_ids
    seen: list[str] = []
    for cid in ids:
        cid = cid.strip()
        if cid and cid not in seen:
            seen.append(cid)
    return seen


def add(chat_id: str) -> bool:
    chat_id = chat_id.strip()
    if not chat_id or chat_id in _extra_ids:
        return False
    _extra_ids.append(chat_id)
    log.info("Telegram recipient added: %s", chat_id)
    return True


def remove(chat_id: str) -> bool:
    chat_id = chat_id.strip()
    if chat_id not in _extra_ids:
        return False
    _extra_ids.remove(chat_id)
    log.info("Telegram recipient removed: %s", chat_id)
    return True


def save() -> None:
    try:
        _FILE.parent.mkdir(exist_ok=True)
        _FILE.write_text(json.dumps(_extra_ids, indent=2), encoding="utf-8")
    except Exception as e:
        log.error("telegram_recipients save: %s", e)


def load() -> None:
    global _extra_ids
    if not _FILE.exists():
        return
    try:
        data = json.loads(_FILE.read_text(encoding="utf-8"))
        if isinstance(data, list):
            _extra_ids = [str(x) for x in data]
        log.info("Telegram recipients loaded: %s", _extra_ids)
    except Exception as e:
        log.error("telegram_recipients load: %s", e)


load()
