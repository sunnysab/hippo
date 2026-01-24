"""Miscellaneous helpers for the project."""

from __future__ import annotations

import json
import re
import unicodedata
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Callable, Sequence

from psycopg.rows import dict_row

from .models import AccountGroup
from .storage import PostgresStorage

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


def fetchall_rows(
    storage: PostgresStorage,
    query: str,
    params: Sequence[Any],
    *,
    normalize: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    with storage.conn.cursor(row_factory=dict_row) as cur:
        cur.execute(query, params)
        rows = cur.fetchall()
    if normalize:
        return [normalize(dict(row)) for row in rows]
    return [dict(row) for row in rows]


def fetchone_row(
    storage: PostgresStorage,
    query: str,
    params: Sequence[Any],
    *,
    normalize: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    with storage.conn.cursor(row_factory=dict_row) as cur:
        cur.execute(query, params)
        row = cur.fetchone()
    if not row:
        return None
    record = dict(row)
    return normalize(record) if normalize else record


def load_meta_json(storage: PostgresStorage, key: str, default: Any) -> Any:
    raw = storage.get_meta(key)
    if not raw:
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return default


def save_meta_json(storage: PostgresStorage, key: str, value: Any) -> None:
    storage.set_meta(key, json.dumps(value, ensure_ascii=False))


def should_skip_by_time(last_synced_at: datetime | None, skip_minutes: int | None) -> bool:
    if not skip_minutes or skip_minutes <= 0:
        return False
    if not last_synced_at:
        return False
    synced_at = last_synced_at
    if synced_at.tzinfo is None:
        synced_at = synced_at.replace(tzinfo=timezone.utc)
    else:
        synced_at = synced_at.astimezone(timezone.utc)
    threshold = datetime.now(timezone.utc) - timedelta(minutes=skip_minutes)
    return synced_at >= threshold


def ensure_default_group(storage: PostgresStorage, *, name: str = 'Default') -> AccountGroup:
    groups = storage.list_groups()
    default_group = next((g for g in groups if g.name == name), None)
    if default_group is None:
        default_group = storage.upsert_group(name)
    default_id = default_group.id
    with storage.conn.cursor() as cur:
        cur.execute(
            'UPDATE accounts SET group_id = %s WHERE group_id IS NULL',
            (default_id,),
        )
    storage.conn.commit()
    return default_group


__all__ = [
    'fetchall_rows',
    'fetchone_row',
    'ensure_default_group',
    'format_table',
    'load_meta_json',
    'parse_iso_date_to_timestamp',
    'parse_iso_datetime_to_timestamp',
    'save_meta_json',
    'should_skip_by_time',
    'slugify',
]
