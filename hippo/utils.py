"""Miscellaneous helpers for the project."""

from __future__ import annotations

import re
import unicodedata
from datetime import UTC, date, datetime, time, timedelta, timezone
from typing import Any

_slug_pattern = re.compile(r'[^a-z0-9-]+')


def slugify(value: str, *, max_length: int = 80) -> str:
    normalized = unicodedata.normalize('NFKD', value).encode('ascii', 'ignore').decode('ascii')
    normalized = normalized.lower()
    normalized = normalized.replace(' ', '-')
    normalized = _slug_pattern.sub('-', normalized)
    normalized = normalized.strip('-')
    if not normalized:
        normalized = 'article'
    if len(normalized) > max_length:
        normalized = normalized[:max_length].rstrip('-')
    return normalized or 'article'


def build_set_clause(
    mapping: dict[str, str],
    updates: dict[str, Any],
) -> tuple[list[str], list[Any]]:
    fields: list[str] = []
    params: list[Any] = []
    for key, column in mapping.items():
        if key in updates:
            fields.append(f'{column} = %s')
            params.append(updates[key])
    return fields, params


def format_table(headers: list[str], rows: list[list[str]]) -> str:
    if not rows:
        return ''
    widths = [len(h) for h in headers]
    for row in rows:
        for idx, cell in enumerate(row):
            widths[idx] = max(widths[idx], len(cell))
    sep = '  '
    lines = [sep.join(h.ljust(widths[idx]) for idx, h in enumerate(headers))]
    lines.append(sep.join('-' * widths[idx] for idx in range(len(headers))))
    for row in rows:
        lines.append(sep.join(row[idx].ljust(widths[idx]) for idx in range(len(headers))))
    return '\n'.join(lines)


def parse_iso_datetime_to_timestamp(value: str | None) -> int | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    return int(parsed.timestamp())


def parse_iso_date_to_timestamp(
    value: str | None,
    *,
    end_of_day: bool = False,
    tz: timezone | None = None,
) -> int | None:
    if not value:
        return None
    parsed = date.fromisoformat(value)
    dt = datetime.combine(parsed, time.max if end_of_day else time.min)
    if tz is not None:
        dt = dt.replace(tzinfo=tz)
    return int(dt.timestamp())


def should_skip_by_time(last_synced_at: datetime | None, skip_minutes: int | None) -> bool:
    if not skip_minutes or skip_minutes <= 0:
        return False
    if not last_synced_at:
        return False
    synced_at = last_synced_at
    synced_at = synced_at.replace(tzinfo=UTC) if synced_at.tzinfo is None else synced_at.astimezone(UTC)
    threshold = datetime.now(UTC) - timedelta(minutes=skip_minutes)
    return synced_at >= threshold


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


__all__ = [
    'build_set_clause',
    'format_table',
    'parse_iso_date_to_timestamp',
    'parse_iso_datetime_to_timestamp',
    'should_skip_by_time',
    'slugify',
    'utc_now_iso',
]
