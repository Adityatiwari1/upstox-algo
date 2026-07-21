"""QuantRunner keeps one rolling history shared by both strategies and
forwards every bar-close snapshot to both engines."""
import tempfile
from pathlib import Path

from uptox.quant.runner import QuantRunner


def _row(nifty, sma_score, ce_ltp=150.0, pe_ltp=80.0, ce_oi_chg=0, pe_oi_chg=0, pcr=1.0,
         ce_iv=12.0, pe_iv=12.0):
    return {"nifty": nifty, "sma_score": sma_score, "ce_ltp": ce_ltp, "pe_ltp": pe_ltp,
            "ce_oi_chg": ce_oi_chg, "pe_oi_chg": pe_oi_chg, "pcr": pcr,
            "ce_iv": ce_iv, "pe_iv": pe_iv}


def _runner():
    r = QuantRunner()
    for strat_id, eng in r.engines.items():
        eng.book_path = Path(tempfile.mkstemp(suffix=".jsonl")[1])
        eng.mode._file = Path(tempfile.mkstemp(suffix=".json")[1])
        eng.mode._mode = "paper"
    return r


def test_has_only_options_dynamics_engine():
    # directional paused 2026-07-21 (negative backtest expectancy) -- see runner.py
    r = _runner()
    assert set(r.engines.keys()) == {"options_dynamics"}


def test_on_bar_appends_to_history():
    r = _runner()
    r.on_bar(_row(24200, 0))
    assert len(r.history) == 1


def test_on_bar_can_trigger_options_dynamics_engine():
    r = _runner()
    r.on_bar(_row(24200, 0, ce_oi_chg=5000, pe_oi_chg=-3000, pcr=0.8, ce_iv=14.0, pe_iv=12.0))
    assert r.engines["options_dynamics"].position is not None
    assert r.engines["options_dynamics"].position["option"] == "CE"


def test_ignores_none_row():
    r = _runner()
    r.on_bar(None)
    assert len(r.history) == 0
