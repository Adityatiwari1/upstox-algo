"""NSE trading day check via exchange-calendars."""
from datetime import date
import exchange_calendars as xcals


def is_trading_day(check_date: date | None = None) -> bool:
    cal = xcals.get_calendar("XNSE")
    d = check_date or date.today()
    return cal.is_session(d.isoformat())


def next_trading_day(from_date: date | None = None) -> date:
    cal = xcals.get_calendar("XNSE")
    d = from_date or date.today()
    session = cal.next_open(d.isoformat())
    return session.date()
