"""
Sends the weekly report on a configured day + time (default Saturday 11:00
IST), independent of the market feed/tick handler — so it still fires on
non-trading days when no ticks are flowing. Configurable live via
notification_settings (weekly_report.weekday / weekly_report.time), which can
be changed through the web API without restarting the app.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

_IST = ZoneInfo("Asia/Kolkata")
_STATE_FILE = Path("logs/weekly_report_state.json")
_POLL_SECONDS = 60

log = logging.getLogger(__name__)


def _load_last_sent_week() -> str | None:
    if not _STATE_FILE.exists():
        return None
    try:
        return json.loads(_STATE_FILE.read_text(encoding="utf-8")).get("last_sent_week")
    except Exception:
        return None


def _save_last_sent_week(week_key: str) -> None:
    try:
        _STATE_FILE.parent.mkdir(exist_ok=True)
        _STATE_FILE.write_text(json.dumps({"last_sent_week": week_key}), encoding="utf-8")
    except Exception as e:
        log.error("weekly_report_scheduler state save error: %s", e)


async def run_forever() -> None:
    from uptox.services import notification_settings as ns
    from uptox.services.telegram_service import notify_weekly_report

    while True:
        try:
            now = datetime.now(_IST)
            weekday_name = str(ns.get_sub("weekly_report", "weekday") or "saturday").lower()
            time_str     = str(ns.get_sub("weekly_report", "time") or "11:00")
            target_wd    = ns.WEEKDAYS.index(weekday_name) if weekday_name in ns.WEEKDAYS else 5
            try:
                hh, mm = (int(x) for x in time_str.split(":"))
            except Exception:
                hh, mm = 11, 0

            week_key = now.strftime("%G-W%V")
            if (
                now.weekday() == target_wd
                and now.hour == hh
                and now.minute == mm
                and _load_last_sent_week() != week_key
            ):
                notify_weekly_report()
                _save_last_sent_week(week_key)
                log.info("Weekly report sent (scheduled %s %s IST)", weekday_name, time_str)
        except Exception as e:
            log.error("weekly_report_scheduler error: %s", e)

        await asyncio.sleep(_POLL_SECONDS)
