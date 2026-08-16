from src.text_tools import initials


def test_initials_are_uppercase() -> None:
    assert initials("Ada Lovelace") == "AL"
    assert initials("grace hopper") == "GH"
