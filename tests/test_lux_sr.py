"""LuxAlgo S/R port: pivot detection (10/10 wings), break/bounce events,
retest window, and the one-trade-per-level guard."""
import time

from uptox.services import lux_sr


def _flat(n, px=24200.0, spread=2.0):
    return [(px, px + spread, px - spread, px) for _ in range(n)]


def _bars_with_pivot_high(level=24260.0):
    bars = _flat(15)
    bars.append((24200.0, level, 24195.0, 24205.0))   # the pivot bar
    bars += _flat(12)                                  # 12 right-wing bars: confirmed
    return bars


def test_pivot_confirmation_needs_full_wings():
    bars = _flat(15) + [(24200.0, 24260.0, 24195.0, 24205.0)] + _flat(5)
    ph, _ = lux_sr.pivots(bars)
    assert ph == []                       # only 5 right bars — not confirmed yet
    ph, _ = lux_sr.pivots(_bars_with_pivot_high())
    assert ph == [24260.0]                # 12 right bars — confirmed


def test_break_up_needs_body():
    bars = _bars_with_pivot_high()
    bars.append((24255.0, 24270.0, 24253.0, 24266.0))  # body-driven cross of 24260
    assert lux_sr.evaluate(bars, 24260.0, 0.0) == ("break_up", 24260.0)
    bars[-1] = (24255.0, 24270.0, 24230.0, 24266.0)     # long lower wick fake
    assert lux_sr.evaluate(bars, 24260.0, 0.0) is None


def test_bounce_up_at_support():
    bars = _flat(30, px=24250.0)
    bars.append((24252.0, 24256.0, 24201.0, 24255.0))  # tags 24200+8, closes green above
    assert lux_sr.evaluate(bars, 0.0, 24200.0) == ("bounce_up", 24200.0)


def test_merge_and_nearest_selection():
    # 5-day pivot soup: near-duplicates collapse (newest wins), active pair =
    # nearest above / nearest below price; a level broken upward reappears as
    # support on the next selection simply because price is now above it
    levels = lux_sr.merge_levels([24310.0, 24312.0, 24450.0], [24100.0, 24210.0])
    assert 24450.0 in levels and 24100.0 in levels and 24210.0 in levels
    assert len([v for v in levels if abs(v - 24311) < 6]) == 1   # 24310/24312 merged
    res, sup = lux_sr._nearest(levels, 24250.0)
    assert sup == 24210.0 and res < 24450.0     # nearest each side, not extremes
    res2, sup2 = lux_sr._nearest(levels, res + 5)   # after an up-break of res
    assert sup2 == res                           # broken resistance flips to support


def test_entry_map_and_once_per_level():
    lux_sr._slot = int(time.time() // 300)
    lux_sr._event = (lux_sr._slot, "break_up", 24260.0)
    lux_sr._traded, lux_sr._traded_date = {}, ""
    assert lux_sr.check_entry("CE") == "lux_break"
    assert lux_sr.check_entry("CE") is None           # same level, same side: once
    assert lux_sr.check_entry("PE") is None           # wrong direction
    lux_sr._event = None


if __name__ == "__main__":
    test_pivot_confirmation_needs_full_wings()
    test_break_up_needs_body()
    test_bounce_up_at_support()
    test_entry_map_and_once_per_level()
    print("OK")
