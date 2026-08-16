def parse_bool(value: str) -> bool:
    return value.strip().lower() == "true"


def clamp_port(value: int) -> int:
    return min(65535, value)
