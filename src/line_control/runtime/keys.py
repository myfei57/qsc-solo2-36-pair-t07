"""Key naming rules for stored values and stream records."""

from __future__ import annotations


def normalize_key(key: str) -> str:
    """Fold a caller supplied key into the internal form."""
    if not isinstance(key, str) or not key:
        raise ValueError("key must be a non-empty string")
    return key.strip().replace("/", "-").replace(" ", "-")


def scope_key(*parts: object) -> str:
    """Join identifier parts into one colon separated key."""
    rendered = [normalize_key(str(part)) for part in parts if str(part)]
    if not rendered:
        raise ValueError("at least one key part is required")
    return ":".join(rendered)


def slug(text: str) -> str:
    """Reduce free text to a short, key safe token."""
    cleaned = [character if character.isalnum() else "-" for character in text.strip().lower()]
    collapsed = "".join(cleaned)
    while "--" in collapsed:
        collapsed = collapsed.replace("--", "-")
    return collapsed.strip("-")
