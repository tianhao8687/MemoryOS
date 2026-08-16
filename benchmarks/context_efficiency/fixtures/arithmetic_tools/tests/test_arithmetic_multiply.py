from src.arithmetic import multiply


def test_multiply_returns_the_product() -> None:
    assert multiply(3, 4) == 12
    assert multiply(-2, 5) == -10
