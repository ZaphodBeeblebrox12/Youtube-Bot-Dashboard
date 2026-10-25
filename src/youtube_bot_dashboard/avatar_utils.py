"""Safe normalization for externally hosted viewer avatar images."""

from urllib.parse import urlsplit


def normalize_avatar_url(value: str | None) -> str | None:
    """Accept only bounded HTTPS image URLs; ignore malformed or unsafe schemes."""
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if not candidate or len(candidate) > 2048 or any(char.isspace() for char in candidate):
        return None
    try:
        parsed = urlsplit(candidate)
        hostname = parsed.hostname
        # Accessing .port catches malformed authority/port syntax as well.
        _ = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme.lower() != "https"
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        return None
    return candidate
