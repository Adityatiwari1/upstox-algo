"""record_bar() must return the row it wrote (or None on skip/error) so
live callers — the quant runner — can consume the same snapshot without
re-deriving it or re-reading the file it was just written to."""
import tempfile
from pathlib import Path
from types import SimpleNamespace

from uptox.services import data_recorder, feed_store, option_chain_service, sma_service
from uptox.services.sma_service import TimeFrame


def _wire(nifty_ltp: float, bank_ltp: float = 55000.0):
    data_recorder._DATA_DIR = Path(tempfile.mkdtemp())
    feed_store.get = lambda key: (
        SimpleNamespace(ltp=nifty_ltp) if "Nifty 50" in key else
        SimpleNamespace(ltp=bank_ltp) if "Nifty Bank" in key else
        SimpleNamespace(ltp=100.0, oi=1000, change_oi=10, iv=15.0)
    )
    option_chain_service.get_option = lambda strike, opt: {
        "ltp": 100.0, "oi": 1000, "change_oi": 10, "iv": 15.0,
    }
    option_chain_service.get_key = lambda strike, opt: f"NSE_FO|{strike}{opt}"
    feed_store.cum_delta = lambda key: 0.0
    sma_service.get_smas = lambda key, tf: {20: nifty_ltp - 10, 50: nifty_ltp - 20}


def test_record_bar_returns_row_shape():
    _wire(24200.0)
    row = data_recorder.record_bar()
    assert row is not None
    for field in ("ts", "nifty", "bank", "atm", "ce_ltp", "pe_ltp", "ce_oi", "pe_oi",
                  "ce_oi_chg", "pe_oi_chg", "ce_iv", "pe_iv", "pcr", "cum_delta",
                  "sma20", "sma50", "sma_score"):
        assert field in row, f"missing field {field}"
    assert row["nifty"] == 24200.0
    assert row["atm"] == 24200


def test_record_bar_returns_none_without_price():
    data_recorder._DATA_DIR = Path(tempfile.mkdtemp())
    feed_store.get = lambda key: None
    assert data_recorder.record_bar() is None
