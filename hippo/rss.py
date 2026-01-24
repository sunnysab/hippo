"""RSS feed generation for Hippo."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import formatdate
from typing import Any, Iterable
from xml.sax.saxutils import escape

from .storage import PostgresStorage, open_storage
from .utils import ensure_default_group, fetchall_rows, parse_iso_date_to_timestamp

DEFAULT_GROUP_NAME = "Default"


@dataclass(slots=True)
class RssItem:
    title: str
    link: str
    guid: str
    pub_date: int | None
    description: str


def _resolve_group_ids(storage: PostgresStorage, names: Iterable[str]) -> list[int]:
    cleaned = [name.strip() for name in names if name and name.strip()]
    if not cleaned:
        return []
    placeholders = ",".join(["%s"] * len(cleaned))
    query = (
        f"SELECT id, name FROM account_groups WHERE name IN ({placeholders})"
        if cleaned
        else "SELECT id, name FROM account_groups"
    )
    rows = fetchall_rows(storage, query, cleaned)
    found = {row["name"]: row["id"] for row in rows}
    missing = [name for name in cleaned if name not in found]
    if missing:
        raise ValueError(f"Group not found: {', '.join(missing)}")
    return [found[name] for name in cleaned]


def _text_to_html(text: str) -> str:
    return escape(text).replace("\n", "<br/>")


def _build_image_src(
    image_base: str | None,
    image_id: int | None,
    orig_url: str | None,
) -> str:
    if image_id and image_base:
        return f"{image_base.rstrip('/')}/api/image/{image_id}"
    if image_id:
        return f"/api/image/{image_id}"
    return orig_url or ""


def _extract_description(raw: Any, image_base: str | None) -> str:
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
    limit: int | None,
    days: int | None,
    since: str | None,
    until: str | None,
    image_base_url: str | None,
) -> list[RssItem]:
    with open_storage() as storage:
        ensure_default_group(storage, name=DEFAULT_GROUP_NAME)
        group_ids = _resolve_group_ids(storage, group_names)

        where: list[str] = []
        params: list[Any] = []

        if group_ids:
            where.append("acc.group_id = ANY(%s)")
            params.append(group_ids)

        since_ts = parse_iso_date_to_timestamp(since, tz=timezone.utc)
        until_ts = parse_iso_date_to_timestamp(until, end_of_day=True, tz=timezone.utc)
        if days:
            now = datetime.now(timezone.utc)
            since_ts = int((now.timestamp() - days * 86400))

        if since_ts is not None:
            where.append("a.publish_at >= %s")
            params.append(since_ts)
        if until_ts is not None:
            where.append("a.publish_at <= %s")
            params.append(until_ts)

        where_sql = f"WHERE {' AND '.join(where)}" if where else ""
        limit_sql = "LIMIT %s"

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

        rows = fetchall_rows(storage, query, params)

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
