"""ATR-scaled MAE stop: -10 flat became max(10, 0.5*ATR) option pts.
2026-07-17: orb CE (ATR 28.5) was stopped at -10 on a 0.57*ATR spot dip,
market then ran +120pt. Allowance that day should have been 14.25."""
import time

from uptox.services import trade_book as tb


def _trade(pnl, atr, mins_ago=1.0):
    tb._open_trade = {"entry_ts": time.time() - mins_ago * 60,
                      "pnl_pts": pnl, "atr_at_entry": atr}


def test_wide_atr_survives_noise():
    _trade(-12.0, 28.5)                      # allowance 14.25
    assert tb.check_time_exit() is None


def test_wide_atr_real_failure_still_exits():
    _trade(-14.5, 28.5)
    assert tb.check_time_exit() == "mae_exit_10min"


def test_quiet_day_unchanged():
    _trade(-10.0, 13.4)                      # max(10, 6.7) = 10, old behavior
    assert tb.check_time_exit() == "mae_exit_10min"


def test_only_first_10_minutes():
    _trade(-15.0, 28.5, mins_ago=12.0)
    assert tb.check_time_exit() != "mae_exit_10min"


if __name__ == "__main__":
    test_wide_atr_survives_noise()
    test_wide_atr_real_failure_still_exits()
    test_quiet_day_unchanged()
    test_only_first_10_minutes()
    tb._open_trade = None
    print("OK")
