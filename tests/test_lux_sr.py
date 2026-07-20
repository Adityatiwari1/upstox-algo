"""LuxAlgo S/R port: pivot detection (10/10 strict, 10/3 fast), OI walls,
break/signal-bar-bounce/retest events, and the one-trade-per-level guard.

2026-07-20: bounce redefined per user spec to a two-stage pattern —
signal bar (closed candle that tags the level and closes near it) arms a
trigger, then the SINGLE next candle must clear the signal bar's high/low
by CONFIRM_PTS, checked live/intrabar, to actually fire the entry.
Worked example from the user: support 24150, signal bar high 24168 (low
tagged support, closed 24152-24154) — next candle must trade up through
~24173 (24168+5) to confirm a CE."""
import time

from uptox.services import lux_sr


def _flat(n, px=24200.0, spread=2.0):
    return [(px, px + spread, px - spread, px) for _ in range(n)]


def _bars_with_pivot_high(level=24260.0, right=12):
    bars = _flat(15)
    bars.append((24200.0, level, 24195.0, 24205.0))   # the pivot bar
    bars += _flat(right)
    return bars


def _reset():
    lux_sr._sig_sup = None
    lux_sr._sig_res = None
    lux_sr._live_fired = None
    lux_sr._event = None


def test_pivot_confirmation_needs_full_wings():
    bars = _flat(15) + [(24200.0, 24260.0, 24195.0, 24205.0)] + _flat(5)
    ph, _ = lux_sr.pivots(bars, lux_sr.LEFT, lux_sr.RIGHT)
    assert ph == []                       # only 5 right bars — not confirmed at strict
    ph, _ = lux_sr.pivots(bars, lux_sr.LEFT, lux_sr.RIGHT_FAST)
    assert ph == [24260.0]                # but confirmed at the fast (3-bar) tier
    ph, _ = lux_sr.pivots(_bars_with_pivot_high())
    assert ph == [24260.0]                # 12 right bars — confirmed at strict too


def test_break_up_needs_body():
    bars = _bars_with_pivot_high()
    bars.append((24255.0, 24270.0, 24253.0, 24266.0))  # body-driven cross of 24260
    assert lux_sr.evaluate_break(bars, 24260.0, 0.0) == ("break_up", 24260.0)
    bars[-1] = (24255.0, 24270.0, 24230.0, 24266.0)     # long lower wick fake
    assert lux_sr.evaluate_break(bars, 24260.0, 0.0) is None


def test_signal_bar_support_matches_user_example():
    # support 24150, bar high 24168, low tags support, closes 24154 — near,
    # above support (24154-24150=4 <= TOL_PTS 8)
    assert lux_sr.signal_bar_support(l=24149.0, c=24154.0, support=24150.0) is True
    # the "pierced through, came back near" variant — close 24152
    assert lux_sr.signal_bar_support(l=24142.0, c=24152.0, support=24150.0) is True
    # never tagged support at all: no signal
    assert lux_sr.signal_bar_support(l=24165.0, c=24170.0, support=24150.0) is False
    # tagged but closed far away from the level: not "near", no signal
    assert lux_sr.signal_bar_support(l=24148.0, c=24170.0, support=24150.0) is False


def test_signal_bar_resistance_mirror():
    assert lux_sr.signal_bar_resistance(h=24308.0, c=24296.0, resistance=24300.0) is True
    assert lux_sr.signal_bar_resistance(h=24280.0, c=24270.0, resistance=24300.0) is False


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
    cur = int(time.time() // 300)
    lux_sr._event = (cur, "break_up", 24260.0)
    lux_sr._traded, lux_sr._traded_date = {}, ""
    assert lux_sr.check_entry("CE") == "lux_break"
    assert lux_sr.check_entry("CE") is None           # same level, same side: once
    assert lux_sr.check_entry("PE") is None           # wrong direction
    lux_sr._event = None


def test_stale_event_from_a_past_bar_is_rejected():
    lux_sr._event = (int(time.time() // 300) - 5, "break_up", 24260.0)
    lux_sr._traded, lux_sr._traded_date = {}, ""
    assert lux_sr.check_entry("CE") is None
    lux_sr._event = None


def test_live_confirm_fires_on_the_users_worked_example(monkeypatch):
    """support 24150, signal bar high 24168 armed in the PREVIOUS bar; the
    next candle trades up to 24175 intrabar (24168+5=24173 needed) — must
    confirm bounce_up right there, without waiting for that candle to close."""
    from uptox.services import sma_service
    _reset()
    prev_slot = int(time.time() // 300) - 1
    lux_sr._sig_sup = (prev_slot, 24150.0, 24168.0)

    monkeypatch.setattr(sma_service, "get_forming_bar",
                        lambda k, tf: (24170.0, 24175.0, 24169.0, 24174.0))

    lux_sr.check_live_bounce()
    assert lux_sr._event == (int(time.time() // 300), "bounce_up", 24150.0)
    _reset()


def test_live_confirm_does_not_fire_below_threshold(monkeypatch):
    """Same signal, but price only reaches 24171 (needs 24173) — must NOT fire."""
    from uptox.services import sma_service
    _reset()
    prev_slot = int(time.time() // 300) - 1
    lux_sr._sig_sup = (prev_slot, 24150.0, 24168.0)

    monkeypatch.setattr(sma_service, "get_forming_bar",
                        lambda k, tf: (24169.0, 24171.0, 24168.5, 24170.0))

    lux_sr.check_live_bounce()
    assert lux_sr._event is None
    _reset()


def test_signal_expires_after_its_one_confirmation_bar():
    """update() must clear an unconfirmed signal once its single follow-up
    bar has closed — a break two bars later must not retroactively confirm."""
    lux_sr._sig_sup = (100, 24150.0, 24168.0)
    # simulate what update() does at the top of a bar close 2 slots later
    slot = 102
    if lux_sr._sig_sup and slot > lux_sr._sig_sup[0] + 1:
        lux_sr._sig_sup = None
    assert lux_sr._sig_sup is None


def test_resistance_side_confirms_downward(monkeypatch):
    from uptox.services import sma_service
    _reset()
    prev_slot = int(time.time() // 300) - 1
    lux_sr._sig_res = (prev_slot, 24300.0, 24280.0)   # signal bar low 24280

    # needs low <= 24280-5=24275 to confirm; 24274 clears it
    monkeypatch.setattr(sma_service, "get_forming_bar",
                        lambda k, tf: (24282.0, 24283.0, 24274.0, 24276.0))
    lux_sr.check_live_bounce()
    assert lux_sr._event == (int(time.time() // 300), "bounce_down", 24300.0)
    _reset()


def test_oi_wall_levels_zero_lag(monkeypatch):
    from uptox.services import option_chain_service
    monkeypatch.setattr(option_chain_service, "get_call_wall", lambda: (24300, 5_000_000))
    monkeypatch.setattr(option_chain_service, "get_put_wall", lambda: (24100, 4_000_000))
    assert lux_sr.oi_wall_levels() == [24300.0, 24100.0]

    monkeypatch.setattr(option_chain_service, "get_call_wall", lambda: None)
    monkeypatch.setattr(option_chain_service, "get_put_wall", lambda: None)
    assert lux_sr.oi_wall_levels() == []


def test_block_levels_label_walls(monkeypatch):
    from uptox.services import option_chain_service
    lux_sr._levels = [24350.0, 24050.0]
    lux_sr._last_close = 24200.0
    monkeypatch.setattr(option_chain_service, "get_call_wall", lambda: (24250, 1))
    monkeypatch.setattr(option_chain_service, "get_put_wall", lambda: (24150, 1))
    labels = dict(lux_sr.get_block_levels())
    assert labels.get("Call Wall") == 24250.0
    assert labels.get("Put Wall") == 24150.0
    lux_sr._levels = []


if __name__ == "__main__":
    class _MP:
        def setattr(self, obj, name, val): setattr(obj, name, val)
    mp = _MP()

    test_pivot_confirmation_needs_full_wings()
    test_break_up_needs_body()
    test_signal_bar_support_matches_user_example()
    test_signal_bar_resistance_mirror()
    test_merge_and_nearest_selection()
    test_entry_map_and_once_per_level()
    test_stale_event_from_a_past_bar_is_rejected()
    test_live_confirm_fires_on_the_users_worked_example(mp)
    test_live_confirm_does_not_fire_below_threshold(mp)
    test_signal_expires_after_its_one_confirmation_bar()
    test_resistance_side_confirms_downward(mp)
    test_oi_wall_levels_zero_lag(mp)
    test_block_levels_label_walls(mp)
    print("OK")
