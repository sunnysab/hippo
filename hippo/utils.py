"""Miscellaneous helpers for the project."""

from __future__ import annotations

import html
import re
import unicodedata
from collections.abc import Mapping
from datetime import UTC, date, datetime, time, timedelta, timezone
from typing import Any

_slug_pattern = re.compile(r'[^a-z0-9-]+')
#: 真正的标签开头（``<a`` / ``</p`` / ``<!--``），不是标题里的字面 ``1+1<1``、``<一>``。
_tag_pattern = re.compile(r'<[a-zA-Z!/]')

#: Deliberately loose: only rejects what is obviously not an address.
EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')


def strip_nul(value: Any) -> Any:
    """去掉 NUL（``\\u0000``）：PG 的 text/jsonb 拒收，而微信的 title/digest 偶尔带一个。"""
    return value.replace('\x00', '') if isinstance(value, str) else value


def unescape_text(value: Any) -> Any:
    """还原文案字段里的 HTML 实体（``户型图&amp;效果图``）：daemon 给的是转义过的纯文本。

    重复转义（``&amp;amp;``）一并还原到稳定为止。含原始标签的片段
    （``<a href="…&amp;mid=…">``）原样返回：那里的 ``&amp;`` 是合法转义，unescape
    会把属性里的 URL 写坏。
    """
    if not isinstance(value, str) or '&' not in value or _tag_pattern.search(value):
        return value
    while True:
        unescaped = html.unescape(value)
        if unescaped == value:
            return value
        value = unescaped


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


def utc_now_dt() -> datetime:
    return datetime.now(UTC)


def to_utc_dt(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def normalize_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def is_http_url(url: str) -> bool:
    from urllib.parse import urlparse

    try:
        parsed = urlparse(url)
    except ValueError, TypeError:
        return False
    return parsed.scheme in ('http', 'https')


#: A WeChat image URL whose ``?`` was lost in the source HTML: ``/640&wxfrom=5``
#: instead of ``/640?wxfrom=5``. The origin answers 400 for the broken form.
_LOST_QUERY_MARKER = re.compile(r'/(\d+)&')


def sanitize_image_url(url: Any) -> str | None:
    """Clean up an image URL scraped from article HTML, or ``None``.

    Article HTML is untrusted and its image attributes are frequently malformed:
    a trailing ``"name"`` or a space-separated alt text after the address, or the
    ``?`` of the query string lost so the path reads ``.../640&wxfrom=5``. The
    origin rejects those forms with a 400, which the reader turns into a broken
    picture, so the address is repaired once here instead of at every fetch.

    Only http(s) addresses that survive the cleanup are returned; a Windows path
    or an empty value is not an address and yields ``None``.
    """
    if not isinstance(url, str):
        return None
    candidate = url.strip()
    if not candidate:
        return None
    # Trim a trailing label glued to the address: a space-separated one
    # (``.../640 "1.png"``) or a quote with the name after it (``...png"1.png``).
    # Splitting on the first quote keeps a URL-encoded or well-formed address whole.
    candidate = candidate.split(' ', 1)[0]
    quote = candidate.find('"')
    if quote != -1 and candidate[:quote].startswith(('http://', 'https://')):
        candidate = candidate[:quote]
    candidate = candidate.strip().strip('"\'')
    if not is_http_url(candidate):
        return None
    # Restore the query marker the source HTML lost: ``/640&wxfrom=5`` -> ``/640?wxfrom=5``.
    match = _LOST_QUERY_MARKER.search(candidate)
    if match and '?' not in candidate:
        candidate = f'{candidate[: match.start()]}/{match.group(1)}?{candidate[match.end() :]}'
    return candidate


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


def should_skip_by_interval(
    last_synced_at: datetime | None,
    sync_interval_days: int | None,
) -> bool:
    """Wait the full interval after the last successful list check."""
    if not last_synced_at:
        return False
    return (datetime.now(UTC) - to_utc_dt(last_synced_at)).total_seconds() < (sync_interval_days or 1) * 86400


def resolve_sync_interval(
    account: Any,
    source_intervals_days: Mapping[str, int],
) -> int:
    """A subscription override wins over its source's default interval."""
    if account.sync_interval_days is not None:
        return account.sync_interval_days
    return source_intervals_days.get(account.source, 1)


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


__all__ = [
    'build_set_clause',
    'format_table',
    'is_http_url',
    'normalize_value',
    'parse_iso_date_to_timestamp',
    'parse_iso_datetime_to_timestamp',
    'resolve_sync_interval',
    'sanitize_image_url',
    'should_skip_by_interval',
    'should_skip_by_time',
    'slugify',
    'strip_nul',
    'to_utc_dt',
    'unescape_text',
    'utc_now_dt',
    'utc_now_iso',
]
