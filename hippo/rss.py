"""RSS feed generation for Hippo."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from email.utils import formatdate
from pathlib import Path
from typing import Any, Iterable, Optional
from xml.sax.saxutils import escape

import psycopg2.extras

from .storage import PostgresStorage, StorageLike, open_storage

DEFAULT_GROUP_NAME = "Default"


@dataclass(slots=True)
class RssItem:
    title: str
    link: str
    guid: str
    pub_date: Optional[int]
    description: str


def _is_postgres(storage: StorageLike) -> bool:
    return isinstance(storage, PostgresStorage)


def _parse_date(value: Optional[str], *, end_of_day: bool = False) -> Optional[int]:
    if not value:
        return None
    parsed = date.fromisoformat(value)
    dt = datetime.combine(parsed, time.max if end_of_day else time.min)
    return int(dt.replace(tzinfo=timezone.utc).timestamp())


def _ensure_default_group(storage: StorageLike) -> int:
    groups = storage.list_groups()
    default_group = next((g for g in groups if g.name == DEFAULT_GROUP_NAME), None)
    if default_group is None:
        default_group = storage.upsert_group(DEFAULT_GROUP_NAME)
    default_id = default_group.id
    if _is_postgres(storage):
        with storage.conn.cursor() as cur:
            cur.execute(
                "UPDATE accounts SET group_id = %s WHERE group_id IS NULL",
                (default_id,),
            )
        storage.conn.commit()
    else:
        storage.conn.execute(
            "UPDATE accounts SET group_id = ? WHERE group_id IS NULL",
            (default_id,),
        )
        storage.conn.commit()
    return default_id


def _fetchall(storage: StorageLike, query: str, params: list[Any]) -> list[dict[str, Any]]:
    if _is_postgres(storage):
        with storage.conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(query, params)
            rows = cur.fetchall()
        return [dict(row) for row in rows]
    rows = storage.conn.execute(query, params).fetchall()
    return [dict(row) for row in rows]


def _resolve_group_ids(storage: StorageLike, names: Iterable[str]) -> list[int]:
    cleaned = [name.strip() for name in names if name and name.strip()]
    if not cleaned:
        return []
    placeholders = ",".join(["%s"] * len(cleaned)) if _is_postgres(storage) else ",".join(["?"] * len(cleaned))
    query = (
        f"SELECT id, name FROM account_groups WHERE name IN ({placeholders})"
        if cleaned
        else "SELECT id, name FROM account_groups"
    )
    rows = _fetchall(storage, query, cleaned)
    found = {row["name"]: row["id"] for row in rows}
    missing = [name for name in cleaned if name not in found]
    if missing:
        raise ValueError(f"Group not found: {', '.join(missing)}")
    return [found[name] for name in cleaned]


def _text_to_html(text: str) -> str:
    return escape(text).replace("\n", "<br/>")


def _build_image_src(
    image_base: Optional[str],
    image_id: Optional[int],
    orig_url: Optional[str],
) -> str:
    if image_id and image_base:
        return f"{image_base.rstrip('/')}/api/image/{image_id}"
    if image_id:
        return f"/api/image/{image_id}"
    return orig_url or ""


def _extract_description(raw: Any, image_base: Optional[str]) -> str:
    if not raw:
        return ""
    blocks = raw
    if isinstance(raw, str):
        try:
            blocks = json.loads(raw)
        except Exception:
            return raw
    if not isinstance(blocks, list):
        return str(blocks)
    parts: list[str] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type == "paragraph" and block.get("text"):
            parts.append(f"<p>{_text_to_html(str(block.get('text')))}</p>")
        elif block_type == "heading" and block.get("text"):
            level = min(max(int(block.get("level") or 2), 2), 4)
            parts.append(
                f"<h{level}>{_text_to_html(str(block.get('text')))}</h{level}>"
            )
        elif block_type == "image":
            src = _build_image_src(image_base, block.get("image_id"), block.get("orig_url"))
            if src:
                alt = _text_to_html(str(block.get("alt") or ""))
                parts.append(f"<img src=\"{src}\" alt=\"{alt}\" />")
        if len(parts) >= 8:
            break
    return "".join(parts)


def query_rss_items(
    *,
    group_names: list[str],
    limit: Optional[int],
    days: Optional[int],
    since: Optional[str],
    until: Optional[str],
    image_base_url: Optional[str],
) -> list[RssItem]:
    with open_storage() as storage:
        _ensure_default_group(storage)
        group_ids = _resolve_group_ids(storage, group_names)
        is_pg = _is_postgres(storage)

        where: list[str] = []
        params: list[Any] = []

        if group_ids:
            if is_pg:
                where.append("acc.group_id = ANY(%s)")
                params.append(group_ids)
            else:
                where.append(f"acc.group_id IN ({','.join(['?'] * len(group_ids))})")
                params.extend(group_ids)

        since_ts = _parse_date(since)
        until_ts = _parse_date(until, end_of_day=True)
        if days:
            now = datetime.now(timezone.utc)
            since_ts = int((now.timestamp() - days * 86400))

        if since_ts is not None:
            where.append("a.publish_at >= %s" if is_pg else "a.publish_at >= ?")
            params.append(since_ts)
        if until_ts is not None:
            where.append("a.publish_at <= %s" if is_pg else "a.publish_at <= ?")
            params.append(until_ts)

        where_sql = f"WHERE {' AND '.join(where)}" if where else ""
        limit_sql = "LIMIT %s" if is_pg else "LIMIT ?"

        query = (
            "SELECT a.id, a.title, a.link, a.publish_at, a.digest, c.content_json"
            " FROM articles a"
            " JOIN accounts acc ON acc.biz = a.biz"
            " LEFT JOIN article_content c ON c.article_pk = a.id"
            f" {where_sql}"
            " ORDER BY a.publish_at IS NULL, a.publish_at DESC, a.id DESC"
        )
        if limit:
            query = f"{query} {limit_sql}"
            params.append(limit)

        rows = _fetchall(storage, query, params)

    items: list[RssItem] = []
    for row in rows:
        description = row.get("digest") or _extract_description(
            row.get("content_json"), image_base_url
        )
        items.append(
            RssItem(
                title=row.get("title") or "",
                link=row.get("link") or "",
                guid=str(row.get("id")),
                pub_date=row.get("publish_at"),
                description=description or "",
            )
        )
    return items


def build_rss_xml(
    *,
    title: str,
    link: str,
    description: str,
    items: list[RssItem],
) -> str:
    now = formatdate(usegmt=True)
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0">',
        '<channel>',
        f'<title>{escape(title)}</title>',
        f'<link>{escape(link)}</link>',
        f'<description>{escape(description)}</description>',
        f'<lastBuildDate>{now}</lastBuildDate>',
    ]
    for item in items:
        pub_date = formatdate(item.pub_date, usegmt=True) if item.pub_date else now
        description = item.description.replace("]]>", "]]]]><![CDATA[>")
        lines.extend(
            [
                '<item>',
                f'<title>{escape(item.title)}</title>',
                f'<link>{escape(item.link)}</link>',
                f'<guid isPermaLink="false">{escape(item.guid)}</guid>',
                f'<pubDate>{pub_date}</pubDate>',
                f'<description><![CDATA[{description}]]></description>',
                '</item>',
            ]
        )
    lines.extend(['</channel>', '</rss>'])
    return "\n".join(lines)


def ensure_xml_path(output: Path) -> Path:
    if output.suffix.lower() != ".xml":
        return output.with_suffix(".xml")
    return output
