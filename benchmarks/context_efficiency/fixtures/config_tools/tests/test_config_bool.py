from src.config_tools import parse_bool


def test_parse_bool_accepts_common_true_values() -> None:
    assert parse_bool("true") is True
    assert parse_bool("YES") is True
    assert parse_bool("on") is True
    assert parse_bool("false") is False
