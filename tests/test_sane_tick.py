"""Freak-tick filter for session extremes: the 2026-07-17 ~24,367 print
(~60pt above market) must be rejected as an extreme-setter, while normal
ticks and sustained real lurches still update the session high."""
from uptox.ui.screens.dashboard import _sane_tick


def test_replay_jul17_freak():
    assert not _sane_tick(24305.0, 24367.0)   # the phantom-high print
    assert _sane_tick(24367.0, 24367.5) is True   # had it kept printing, accepted


def test_normal_and_edge():
    assert _sane_tick(24300.0, 24310.0)
    assert _sane_tick(24300.0, 24340.0)       # exactly 40 = allowed
    assert not _sane_tick(24300.0, 24341.0)
    assert _sane_tick(0.0, 24300.0)           # first tick after restart


if __name__ == "__main__":
    test_replay_jul17_freak()
    test_normal_and_edge()
    print("OK")
