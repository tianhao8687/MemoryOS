from src.text_tools import slugify


def test_slugify_uses_hyphens() -> None:
    assert slugify("Memory OS") == "memory-os"
    assert slugify("  Local Model  ") == "local-model"
