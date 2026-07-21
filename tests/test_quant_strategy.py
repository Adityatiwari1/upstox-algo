"""Signal dataclass shape and Strategy ABC contract."""
from uptox.quant.strategy import Signal, Strategy


def test_signal_fields():
    s = Signal(option="CE", reason="test", score=1.0, strategy_id="x")
    assert s.option == "CE"
    assert s.reason == "test"
    assert s.score == 1.0
    assert s.strategy_id == "x"


def test_strategy_is_abstract():
    try:
        Strategy()
        assert False, "Strategy() should not be instantiable"
    except TypeError:
        pass


def test_concrete_strategy_must_implement_generate_signal():
    class Dummy(Strategy):
        strategy_id = "dummy"

        def generate_signal(self, history):
            return Signal(option="PE", reason="always", score=0.0, strategy_id=self.strategy_id)

    d = Dummy()
    sig = d.generate_signal([])
    assert sig.option == "PE"
