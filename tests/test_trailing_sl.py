"""Replay of the 2026-07-16 trade: fast PE peaked +11.3, a 6.9pt option
flutter clipped the 40% lock (floor 144.1) 2 minutes before a further 35pt
spot leg. With ATR breathing room the dip to 144.0 must survive and the
trade must ride the next leg, still banking >=25% of peak on the real exit."""
from uptox.services import trailing_sl_service as tsl

KEY = "NSE_FO|57345"


def _open_trade(atr_pts):
    tsl.exit_trade()
    tsl.set_entry(
        139.6, "PE", KEY,
        target=166.6, initial_sl=121.6,
        regime="CONSOLIDATING", atr_pts=atr_pts,
    )


def test_atr_breathing_survives_noise_pullback():
    _open_trade(atr_pts=13.4)
    assert not tsl.update(KEY, 150.9)        # fast +11.3 -> velocity upgrade
    assert not tsl.update(KEY, 144.0)        # 6.9pt flutter — must NOT stop out
    assert not tsl.update(KEY, 160.0)        # rides the continuation leg
    floor = tsl.get_status()["sl_floor"]
    assert floor >= 139.6 + 0.25 * (160.0 - 139.6)   # keep-min banked
    assert tsl.update(KEY, floor - 0.1)      # real reversal still stops
    exit_gain = (floor - 0.1) - 139.6
    assert exit_gain > 4.4                   # beats the 2026-07-16 realized exit
    tsl.exit_trade()


def test_no_atr_keeps_old_behavior():
    _open_trade(atr_pts=0.0)
    assert not tsl.update(KEY, 150.9)
    assert tsl.update(KEY, 144.0)            # old 40% lock: floor 144.1 stops it
    tsl.exit_trade()


def test_slow_grinder_gets_breathing_room():
    """2026-07-17 trade 1 replay: pdh_break CE 121, ATR 26.2, NORMAL regime.
    Peaked 133.2 slowly (no velocity upgrade), the 50% lock floor 127.1
    stopped it at 126.3 (+5.3) and the contract later printed ~+34."""
    tsl.exit_trade()
    tsl.set_entry(121.0, "CE", KEY, target=157.1, initial_sl=103.0,
                  regime="NORMAL", atr_pts=26.2)
    tsl._entry_ts -= 400                     # outside the 3-min velocity window
    assert not tsl.update(KEY, 133.2)        # peak +12.2, slow
    assert not tsl.update(KEY, 126.3)        # old floor 127.1 stopped here
    assert not tsl.update(KEY, 150.0)        # rides the trend leg
    tsl.exit_trade()


def test_wide_atr_never_unlocks_below_keep_min():
    """Jul-8 guard: wide ATR must not let an armed winner bleed to scratch."""
    tsl.exit_trade()
    tsl.set_entry(100.0, "CE", KEY, target=140.0, initial_sl=88.0,
                  regime="NORMAL", atr_pts=30.0)
    tsl._entry_ts -= 400
    assert not tsl.update(KEY, 108.0)        # peak +8: lock armed
    assert tsl.get_status()["sl_floor"] >= 102.0   # ≥ entry + 25% of gain
    tsl.exit_trade()


if __name__ == "__main__":
    test_atr_breathing_survives_noise_pullback()
    test_no_atr_keeps_old_behavior()
    test_slow_grinder_gets_breathing_room()
    test_wide_atr_never_unlocks_below_keep_min()
    print("OK")
