"""Miscellaneous helpers for the CLI."""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Optional

from .config import DOWNLOAD_ROOT

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


def ensure_directory(path: Path | None = None) -> Path:
    target = path or DOWNLOAD_ROOT
    target.mkdir(parents=True, exist_ok=True)
    return target


def timestamp_to_datestr(ts: Optional[int]) -> str:
    if not ts:
        return datetime.utcnow().strftime("%Y-%m-%d")
    return datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d")


__all__ = ["slugify", "ensure_directory", "timestamp_to_datestr"]
