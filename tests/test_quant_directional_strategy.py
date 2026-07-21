"""Directional signal: SMA-score at the extreme (+2/-2) confirmed by the
last bar's spot momentum in the same direction. Needs >=2 history rows."""
from uptox.quant.directional_strategy import DirectionalStrategy


def _row(nifty, sma_score):
    return {"nifty": nifty, "sma_score": sma_score}


def test_no_signal_with_less_than_2_rows():
    s = DirectionalStrategy()
    assert s.generate_signal([]) is None
    assert s.generate_signal([_row(24200, 2)]) is None


def test_ce_signal_on_score_plus2_and_uptick():
    s = DirectionalStrategy()
    history = [_row(24190, 2), _row(24200, 2)]  # moved up, score maxed bullish
    sig = s.generate_signal(history)
    assert sig is not None
    assert sig.option == "CE"
    assert sig.strategy_id == "directional"


def test_pe_signal_on_score_minus2_and_downtick():
    s = DirectionalStrategy()
    history = [_row(24210, -2), _row(24200, -2)]  # moved down, score maxed bearish
    sig = s.generate_signal(history)
    assert sig is not None
    assert sig.option == "PE"


def test_no_signal_when_score_not_extreme():
    s = DirectionalStrategy()
    history = [_row(24190, 1), _row(24200, 1)]
    assert s.generate_signal(history) is None


def test_no_signal_when_score_extreme_but_price_flat():
    s = DirectionalStrategy()
    history = [_row(24200, 2), _row(24200, 2)]  # no momentum confirmation
    assert s.generate_signal(history) is None
