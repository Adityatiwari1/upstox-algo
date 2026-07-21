"""Options-dynamics signal: OI buildup direction (using this codebase's
existing convention from option_chain_service.get_oi_signal — CE-OI-up +
PE-OI-down reads bullish) confirmed by PCR level and matching IV move."""
from uptox.quant.options_dynamics_strategy import OptionsDynamicsStrategy


def _row(ce_oi_chg, pe_oi_chg, pcr, ce_iv, pe_iv, ce_ltp=100.0, pe_ltp=100.0):
    return {"ce_oi_chg": ce_oi_chg, "pe_oi_chg": pe_oi_chg, "pcr": pcr,
            "ce_iv": ce_iv, "pe_iv": pe_iv, "ce_ltp": ce_ltp, "pe_ltp": pe_ltp}


def test_no_signal_on_empty_history():
    s = OptionsDynamicsStrategy()
    assert s.generate_signal([]) is None


def test_ce_signal_on_bullish_oi_low_pcr():
    s = OptionsDynamicsStrategy()
    history = [_row(ce_oi_chg=5000, pe_oi_chg=-3000, pcr=0.8, ce_iv=14.0, pe_iv=12.0)]
    sig = s.generate_signal(history)
    assert sig is not None
    assert sig.option == "CE"
    assert sig.strategy_id == "options_dynamics"


def test_pe_signal_on_bearish_oi_high_pcr():
    s = OptionsDynamicsStrategy()
    history = [_row(ce_oi_chg=-4000, pe_oi_chg=6000, pcr=1.4, ce_iv=11.0, pe_iv=13.0)]
    sig = s.generate_signal(history)
    assert sig is not None
    assert sig.option == "PE"


def test_no_signal_when_oi_direction_conflicts_with_pcr():
    s = OptionsDynamicsStrategy()
    # bullish OI pattern but PCR still high (>1.0) — not confirmed
    history = [_row(ce_oi_chg=5000, pe_oi_chg=-3000, pcr=1.3, ce_iv=14.0, pe_iv=12.0)]
    assert s.generate_signal(history) is None


def test_no_signal_when_oi_moves_same_direction():
    s = OptionsDynamicsStrategy()
    history = [_row(ce_oi_chg=3000, pe_oi_chg=3000, pcr=1.0, ce_iv=12.0, pe_iv=12.0)]
    assert s.generate_signal(history) is None


# Price-direction confirmation (long vs short buildup) is paused -- see
# options_dynamics_strategy.py. These two cases documented the theory but
# backtested worse; kept here, unused, so re-enabling the code makes them
# meaningful again instead of having to rederive the fixtures.
#
# def test_no_signal_ce_pattern_but_short_buildup():
#     s = OptionsDynamicsStrategy()
#     history = [
#         _row(ce_oi_chg=5000, pe_oi_chg=-3000, pcr=0.8, ce_iv=14.0, pe_iv=12.0, ce_ltp=105.0),
#         _row(ce_oi_chg=5000, pe_oi_chg=-3000, pcr=0.8, ce_iv=14.0, pe_iv=12.0, ce_ltp=100.0),
#     ]
#     assert s.generate_signal(history) is None
#
#
# def test_no_signal_pe_pattern_but_short_buildup():
#     s = OptionsDynamicsStrategy()
#     history = [
#         _row(ce_oi_chg=-4000, pe_oi_chg=6000, pcr=1.4, ce_iv=11.0, pe_iv=13.0, pe_ltp=105.0),
#         _row(ce_oi_chg=-4000, pe_oi_chg=6000, pcr=1.4, ce_iv=11.0, pe_iv=13.0, pe_ltp=100.0),
#     ]
#     assert s.generate_signal(history) is None
