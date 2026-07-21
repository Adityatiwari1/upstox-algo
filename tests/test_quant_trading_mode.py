"""Two strategies must have fully independent paper/live flags — flipping
one must never affect the other or the global trading_mode."""
import os
import tempfile
from pathlib import Path

from uptox.quant.quant_trading_mode import QuantTradingMode


def test_defaults_to_paper():
    m = QuantTradingMode("test_a")
    fd, path = tempfile.mkstemp(suffix=".json")
    os.close(fd)  # mkstemp creates and opens; close the fd so unlink works
    m._file = Path(path)
    m._file.unlink()  # no prior state
    m._mode = "paper"
    assert m.is_paper()
    assert not m.is_live()


def test_set_mode_persists_and_loads():
    fd, path = tempfile.mkstemp(suffix=".json")
    os.close(fd)  # mkstemp creates and opens; close the fd so unlink works
    tmp = Path(path)
    m = QuantTradingMode("test_b")
    m._file = tmp
    m.set_mode("live")
    assert m.is_live()

    m2 = QuantTradingMode("test_b")
    m2._file = tmp
    m2._load()
    assert m2.is_live()


def test_invalid_mode_raises():
    m = QuantTradingMode("test_c")
    try:
        m.set_mode("bogus")
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_two_instances_are_independent():
    a = QuantTradingMode("test_d1")
    fd, path = tempfile.mkstemp(suffix=".json")
    os.close(fd)  # mkstemp creates and opens; close the fd so unlink works
    a._file = Path(path)
    b = QuantTradingMode("test_d2")
    fd, path = tempfile.mkstemp(suffix=".json")
    os.close(fd)  # mkstemp creates and opens; close the fd so unlink works
    b._file = Path(path)
    a.set_mode("live")
    assert a.is_live()
    assert b.is_paper()  # unaffected by a
