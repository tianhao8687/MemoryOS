from src.calculator import add


def test_add_returns_arithmetic_sum() -> None:
    assert add(2, 3) == 5
    assert add(-4, 1) == -3
