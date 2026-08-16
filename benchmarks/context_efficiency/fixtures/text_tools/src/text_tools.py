def slugify(value: str) -> str:
    return value.strip().lower().replace(" ", "_")


def initials(value: str) -> str:
    return "".join(part[0].lower() for part in value.split())
