from src.arithmetic import add


def test_add_returns_the_sum() -> None:
    assert add(2, 3) == 5
    assert add(-4, 1) == -3
