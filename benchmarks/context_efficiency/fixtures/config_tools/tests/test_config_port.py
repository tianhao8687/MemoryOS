from src.config_tools import clamp_port


def test_clamp_port_stays_in_the_valid_range() -> None:
    assert clamp_port(-1) == 1
    assert clamp_port(8080) == 8080
    assert clamp_port(70000) == 65535
