import time

import pytest

from groundstation.convert import compile_equation
from groundstation.errors import ConfigError


def test_voltage_equation():
    assert compile_equation("RAW * 0.01")(370) == pytest.approx(3.70)


def test_temperature_equation():
    fn = compile_equation("(RAW * 0.1) - 40")
    assert fn(650) == pytest.approx(25.0)
    assert fn(-100) == pytest.approx(-50.0)


@pytest.mark.parametrize("expr", [
    "__import__('os').system('x')", "RAW.real", "open('f')", "RAW ** 2",
    "9**9**9", "FOO + 1", "lambda: 1", "RAW if 1 else 2", "'a'*3", "RAW[0]",
])
def test_rejected_quickly(expr):
    start = time.perf_counter()
    with pytest.raises(ConfigError):
        compile_equation(expr)
    assert time.perf_counter() - start < 0.5


def test_division_by_zero_left_to_caller():
    fn = compile_equation("1 / RAW")
    with pytest.raises(ZeroDivisionError):
        fn(0)
