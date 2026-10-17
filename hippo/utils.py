"""Miscellaneous helpers for the project."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import UTC, date, datetime, time, timedelta, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .storage import PostgresStorage

_slug_pattern = re.compile(r'[^a-z0-9-]+')

#: Deliberately loose: only rejects what is obviously not an address.
EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')


def strip_nul(value: Any) -> Any:
    """去掉 NUL（``\\u0000``）：PG 的 text/jsonb 拒收，而微信的 title/digest 偶尔带一个。"""
    return value.replace('\x00', '') if isinstance(value, str) else value


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


def _account_spread_hash(biz: str) -> int:
    """Stable hash for deterministic day-of-cycle assignment."""
    return int(hashlib.sha256(biz.encode()).hexdigest()[:8], 16)


def should_skip_by_interval(
    last_synced_at: datetime | None,
    sync_interval_days: int | None,
    biz: str,
) -> bool:
    """Return True if account should be skipped based on its sync interval.

    - NULL or <= 1: never skip (sync every run)
    - Otherwise: allow sync on assigned day ±1 within the interval cycle.
      Accounts past their interval always sync (catch-up).
    """
    if not sync_interval_days or sync_interval_days <= 1:
        return False
    if not last_synced_at:
        return False

    synced_at = last_synced_at.replace(tzinfo=UTC) if last_synced_at.tzinfo is None else last_synced_at.astimezone(UTC)
    now = datetime.now(UTC)
    days_since_last_sync = (now - synced_at).days

    # Already past the interval — sync immediately (catch-up)
    if days_since_last_sync >= sync_interval_days:
        return False

    # Spread accounts across the interval cycle using a stable hash.
    # Allow a ±1 day window around the assigned slot for graceful catch-up.
    epoch_days = (now.date() - date(2020, 1, 1)).days
    assigned_slot = _account_spread_hash(biz) % sync_interval_days
    today_slot = epoch_days % sync_interval_days

    return all(today_slot != (assigned_slot + offset) % sync_interval_days for offset in (0, 1, -1))


def resolve_auto_interval(days_since_last_publish: int | None) -> int:
    """Map days since last publish to a sync interval in days."""
    if days_since_last_publish is None or days_since_last_publish <= 2:
        return 1
    if days_since_last_publish <= 7:
        return 3
    if days_since_last_publish <= 30:
        return 7
    if days_since_last_publish <= 90:
        return 14
    return 30


async def resolve_sync_interval(
    storage: PostgresStorage,
    account: Any,
) -> int:
    """Resolve effective sync interval for an account.

    Manual override (sync_interval_days) takes priority.
    Falls back to auto-detection from article publish history.
    """
    if account.sync_interval_days is not None:
        return account.sync_interval_days

    try:
        latest_publish_at = await storage.accounts.get_latest_publish_at(account.biz)
    except Exception:
        return 1

    if latest_publish_at is None:
        return 1

    latest = (
        latest_publish_at.replace(tzinfo=UTC) if latest_publish_at.tzinfo is None else latest_publish_at.astimezone(UTC)
    )
    days_since = (datetime.now(UTC) - latest).days
    return resolve_auto_interval(days_since)


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


__all__ = [
    'build_set_clause',
    'format_table',
    'is_http_url',
    'normalize_value',
    'parse_iso_date_to_timestamp',
    'parse_iso_datetime_to_timestamp',
    'resolve_auto_interval',
    'resolve_sync_interval',
    'sanitize_image_url',
    'should_skip_by_interval',
    'should_skip_by_time',
    'slugify',
    'strip_nul',
    'to_utc_dt',
    'utc_now_dt',
    'utc_now_iso',
]
