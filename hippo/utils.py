"""Miscellaneous helpers for the CLI."""

from __future__ import annotations

import re
import unicodedata

_slug_pattern = re.compile(r"[^a-z0-9-]+")


def slugify(value: str, *, max_length: int = 80) -> str:
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    normalized = normalized.lower()
    normalized = normalized.replace(" ", "-")
    normalized = _slug_pattern.sub("-", normalized)
    normalized = normalized.strip("-")
    if not normalized:
        normalized = "article"
    if len(normalized) > max_length:
        normalized = normalized[:max_length].rstrip("-")
    return normalized or "article"


__all__ = ["slugify"]
