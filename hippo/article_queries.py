"""Article query building, normalization, and data-access helpers."""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any

try:
    import jieba
except Exception:  # pragma: no cover - optional fallback
    jieba = None

from .storage import PostgresStorage, fetchall_rows, fetchone_row
from .exceptions import ApiError
from .image_hashes import IMAGE_HASH_ALGO, ensure_image_hash, fetch_image_bytes
from .rss import build_rss_xml, query_rss_items
from .utils import parse_iso_date_to_timestamp, utc_now_iso

logger = logging.getLogger(__name__)


ARTICLE_SORT_PUBLISH_AT_DESC = 'publish_at_desc'
ARTICLE_SORT_RELEVANCE_DESC = 'relevance_desc'
_ARTICLE_SORT_VALUES = {ARTICLE_SORT_PUBLISH_AT_DESC, ARTICLE_SORT_RELEVANCE_DESC}
_ITEM_SHOW_TYPE_VALUES = {0, 5, 6, 7, 8, 10, 11, 17}
_ARTICLE_EXCLUDE_KEYWORD_LIMIT = 20
_SYNC_MODES = {'incremental', 'recent', 'full', 'range'}


def _build_item_show_type_where_clause(item_show_type: int) -> tuple[str, list[int]]:
    if item_show_type == 0:
        return '(a.item_show_type = %s OR a.item_show_type IS NULL)', [0]
    return 'a.item_show_type = %s', [item_show_type]


def _coalesce_item_show_type(value: Any) -> int | None:
    if value is None:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _normalize_item_show_type(value: Any) -> int | None:
    if value in (None, ''):
        return None
    try:
        normalized = int(value)
    except (TypeError, ValueError) as exc:
        raise ApiError('Invalid item_show_type', status=400) from exc
    if normalized not in _ITEM_SHOW_TYPE_VALUES:
        raise ApiError('Invalid item_show_type', status=400)
    return normalized


def _normalize_sync_mode(value: Any) -> str | None:
    if value in (None, ""):
        return None
    mode = str(value).strip().lower()
    if not mode:
        return None
    if mode not in _SYNC_MODES:
        raise ApiError('Invalid sync mode', status=400)
    return mode


def _normalize_recent_days(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        days = int(value)
    except (TypeError, ValueError) as exc:
        raise ApiError('Invalid recent days') from exc
    if days < 1:
        raise ApiError('Invalid recent days', status=400)
    return days


def _normalize_article_sort(value: str | None, *, has_query: bool) -> str:
    if value in (None, ''):
        return ARTICLE_SORT_RELEVANCE_DESC if has_query else ARTICLE_SORT_PUBLISH_AT_DESC
    sort = value.strip().lower()
    if sort not in _ARTICLE_SORT_VALUES:
        raise ApiError('Invalid sort', status=400)
    if sort == ARTICLE_SORT_RELEVANCE_DESC and not has_query:
        return ARTICLE_SORT_PUBLISH_AT_DESC
    return sort


def _split_article_query_terms(query: str | None) -> list[str]:
    if not query:
        return []
    terms = [part.strip() for part in re.split(r'\s+', query) if part.strip()]
    if not terms:
        return []
    deduped: list[str] = []
    seen: set[str] = set()
    for term in terms:
        if term in seen:
            continue
        seen.add(term)
        deduped.append(term)
    return deduped


def _build_article_search_tsquery(query: str | None) -> tuple[str, list[str]]:
    query_text = query.strip() if query else ''
    if not query_text:
        return '', []
    terms = _split_article_query_terms(query_text)
    if len(terms) <= 1:
        return "plainto_tsquery('jiebaqry', %s)", [query_text]
    tsquery_sql = ' || '.join(["plainto_tsquery('jiebaqry', %s)"] * len(terms))
    return f'({tsquery_sql})', terms


def _split_article_exclude_keywords(raw: str | None) -> list[str]:
    if not raw:
        return []
    keywords: list[str] = []
    seen: set[str] = set()
    for chunk in re.split(r'[,;\n]+', raw):
        term = chunk.strip()
        dedupe_key = term.lower()
        if not term or dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        keywords.append(term)
        if len(keywords) >= _ARTICLE_EXCLUDE_KEYWORD_LIMIT:
            break
    return keywords


def _build_article_exclude_keywords_where_clause(exclude_keywords: list[str] | None) -> tuple[str, list[str]]:
    if not exclude_keywords:
        return '', []
    clauses: list[str] = []
    params: list[str] = []
    for keyword in exclude_keywords:
        pattern = f'%{keyword.lower()}%'
        clauses.append(
            "("
            "LOWER(COALESCE(a.title, '')) LIKE %s"
            " OR LOWER(COALESCE(a.digest, '')) LIKE %s"
            " OR LOWER(COALESCE(a.author, '')) LIKE %s"
            ")"
        )
        params.extend([pattern, pattern, pattern])
    return f"NOT ({' OR '.join(clauses)})", params


def _parse_date(value: str | None, *, end_of_day: bool = False) -> int | None:
    try:
        return parse_iso_date_to_timestamp(value, end_of_day=end_of_day)
    except ValueError as exc:
        raise ApiError(f"Invalid date: {value}") from exc


def _normalize_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _normalize_record(record: dict[str, Any]) -> dict[str, Any]:
    return {key: _normalize_value(value) for key, value in record.items()}


def _tokenize_query(text: str) -> list[str]:
    trimmed = text.strip()
    if not trimmed:
        return []
    if jieba:
        tokens = [token.strip() for token in jieba.lcut(trimmed) if token.strip()]
        seen: set[str] = set()
        ordered: list[str] = []
        for token in tokens:
            if token in seen:
                continue
            seen.add(token)
            ordered.append(token)
        return ordered[:12]
    chunks = re.findall(r"[\u4e00-\u9fff]+|[A-Za-z0-9_]+", trimmed)
    tokens: list[str] = []
    for chunk in chunks:
        if re.fullmatch(r"[\u4e00-\u9fff]+", chunk):
            tokens.extend(list(chunk))
        else:
            tokens.append(chunk)
    return tokens[:12]


def _build_article_where_clause(
    *,
    group_id: int | None,
    biz: str | None,
    item_show_type: int | None,
    query: str | None,
    exclude_keywords: list[str] | None,
    since_ts: int | None,
    until_ts: int | None,
    article_id: str | None = None,
) -> tuple[str, list[Any], str, list[str]]:
    where: list[str] = []
    params: list[Any] = []
    query_text = query.strip() if query else ''
    query_tsquery_sql, query_tsquery_params = _build_article_search_tsquery(query_text)

    if article_id:
        where.append('a.article_id = %s')
        params.append(article_id)
    if group_id is not None:
        where.append('acc.group_id = %s')
        params.append(group_id)
    if biz:
        where.append('a.biz = %s')
        params.append(biz)
    if item_show_type is not None:
        clause, clause_params = _build_item_show_type_where_clause(item_show_type)
        where.append(clause)
        params.extend(clause_params)
    if query_tsquery_sql:
        where.append(f'a.search_vector @@ {query_tsquery_sql}')
        params.extend(query_tsquery_params)
    exclude_clause, exclude_params = _build_article_exclude_keywords_where_clause(exclude_keywords)
    if exclude_clause:
        where.append(exclude_clause)
        params.extend(exclude_params)
    if since_ts is not None:
        where.append('a.publish_at >= %s')
        params.append(since_ts)
    if until_ts is not None:
        where.append('a.publish_at <= %s')
        params.append(until_ts)

    where_sql = f"WHERE {' AND '.join(where)}" if where else ''
    return where_sql, params, query_tsquery_sql, query_tsquery_params


def _build_article_query(
    *,
    storage: PostgresStorage,
    group_id: int | None,
    biz: str | None,
    item_show_type: int | None,
    query: str | None,
    exclude_keywords: list[str] | None,
    since_ts: int | None,
    until_ts: int | None,
    sort_mode: str,
    limit: int,
    offset: int,
    article_id: str | None = None,
) -> tuple[str, list[Any]]:
    select_params: list[Any] = []
    rank_select = ""
    order_sql = "ORDER BY a.publish_at DESC NULLS LAST, a.id DESC"

    where_sql, params, query_tsquery_sql, query_tsquery_params = _build_article_where_clause(
        group_id=group_id,
        biz=biz,
        item_show_type=item_show_type,
        query=query,
        exclude_keywords=exclude_keywords,
        since_ts=since_ts,
        until_ts=until_ts,
        article_id=article_id,
    )

    if sort_mode == ARTICLE_SORT_RELEVANCE_DESC and query_tsquery_sql:
        rank_select = f", ts_rank(a.search_vector, {query_tsquery_sql}) AS rank"
        select_params.extend(query_tsquery_params)
        order_sql = "ORDER BY rank DESC, a.publish_at DESC NULLS LAST, a.id DESC"

    limit_sql = "LIMIT %s OFFSET %s"

    image_sql = (
        "LEFT JOIN LATERAL ("
        "  SELECT id FROM article_images i"
        "  WHERE i.article_pk = a.id AND i.s3_key IS NOT NULL AND i.s3_key <> ''"
        "  ORDER BY (i.kind = 'cover') DESC, i.position ASC"
        "  LIMIT 1"
        ") img ON TRUE"
    )
    image_select = "img.id AS image_id"

    query_sql = (
        "SELECT a.id, a.biz, a.article_id, a.title, a.item_show_type, a.author, a.digest, a.cover, a.link,"
        " a.source_url, a.publish_at, a.created_at,"
        " acc.nickname AS account_nickname, acc.alias AS account_alias,"
        " acc.round_head_img AS account_avatar,"
        " acc.group_id, g.name AS group_name,"
        f" {image_select}"
        f"{rank_select}"
        " FROM articles a"
        " JOIN accounts acc ON acc.biz = a.biz"
        " LEFT JOIN account_groups g ON g.id = acc.group_id"
        f" {image_sql}"
        f" {where_sql}"
        f" {order_sql}"
        f" {limit_sql}"
    )
    params = select_params + params + [limit, offset]
    return query_sql, params


def _count_articles(
    *,
    storage: PostgresStorage,
    group_id: int | None,
    biz: str | None,
    item_show_type: int | None,
    query: str | None,
    exclude_keywords: list[str] | None,
    since_ts: int | None,
    until_ts: int | None,
    article_id: str | None = None,
) -> int:
    where_sql, params, _tsquery_sql, _tsquery_params = _build_article_where_clause(
        group_id=group_id,
        biz=biz,
        item_show_type=item_show_type,
        query=query,
        exclude_keywords=exclude_keywords,
        since_ts=since_ts,
        until_ts=until_ts,
        article_id=article_id,
    )
    row = fetchone_row(
        storage,
        (
            'SELECT COUNT(*) AS total'
            ' FROM articles a'
            ' JOIN accounts acc ON acc.biz = a.biz'
            f' {where_sql}'
        ),
        params,
        normalize=_normalize_record,
    )
    return int(row.get('total') or 0) if row else 0


def _count_article_item_show_type_facets(
    *,
    storage: PostgresStorage,
    group_id: int | None,
    biz: str | None,
    query: str | None,
    exclude_keywords: list[str] | None,
    since_ts: int | None,
    until_ts: int | None,
    article_id: str | None = None,
) -> list[dict[str, int]]:
    where_sql, params, _tsquery_sql, _tsquery_params = _build_article_where_clause(
        group_id=group_id,
        biz=biz,
        item_show_type=None,
        query=query,
        exclude_keywords=exclude_keywords,
        since_ts=since_ts,
        until_ts=until_ts,
        article_id=article_id,
    )
    rows = fetchall_rows(
        storage,
        (
            'SELECT COALESCE(a.item_show_type, 0) AS item_show_type, COUNT(*) AS total'
            ' FROM articles a'
            ' JOIN accounts acc ON acc.biz = a.biz'
            f' {where_sql}'
            ' GROUP BY COALESCE(a.item_show_type, 0)'
            ' ORDER BY COALESCE(a.item_show_type, 0) ASC'
        ),
        params,
        normalize=_normalize_record,
    )
    order_map = {value: index for index, value in enumerate(sorted(_ITEM_SHOW_TYPE_VALUES))}
    facet_counts: dict[int, int] = {}
    for row in rows:
        item_show_type = row.get('item_show_type')
        total = row.get('total')
        if item_show_type is None:
            item_show_type = 0
        try:
            normalized_type = int(item_show_type)
            normalized_total = int(total)
        except (TypeError, ValueError):
            continue
        if normalized_type not in _ITEM_SHOW_TYPE_VALUES or normalized_total <= 0:
            continue
        facet_counts[normalized_type] = facet_counts.get(normalized_type, 0) + normalized_total
    facets = [
        {'item_show_type': item_show_type, 'count': count}
        for item_show_type, count in facet_counts.items()
    ]
    facets.sort(key=lambda item: order_map.get(item['item_show_type'], 999))
    return facets


def _get_cached_article_total(
    storage: PostgresStorage,
    *,
    group_id: int | None,
    biz: str | None,
) -> int:
    if biz:
        row = fetchone_row(
            storage,
            'SELECT group_id, article_count FROM accounts WHERE biz = %s',
            [biz],
            normalize=_normalize_record,
        )
        if not row:
            return 0
        if group_id is not None and row.get('group_id') != group_id:
            return 0
        return int(row.get('article_count') or 0)
    if group_id is not None:
        row = fetchone_row(
            storage,
            'SELECT article_count FROM account_groups WHERE id = %s',
            [group_id],
            normalize=_normalize_record,
        )
        return int(row.get('article_count') or 0) if row else 0
    row = fetchone_row(
        storage,
        'SELECT COALESCE(SUM(article_count), 0) AS total FROM accounts',
        [],
        normalize=_normalize_record,
    )
    return int(row.get('total') or 0) if row else 0


def _list_articles(
    storage: PostgresStorage,
    *,
    group_id: int | None,
    biz: str | None,
    item_show_type: int | None,
    query: str | None,
    exclude_keywords: list[str] | None,
    since_ts: int | None,
    until_ts: int | None,
    sort_mode: str,
    page: int,
    page_size: int,
    article_id: str | None = None,
) -> dict[str, Any]:
    offset = max(page - 1, 0) * page_size
    query_sql, params = _build_article_query(
        storage=storage,
        group_id=group_id,
        biz=biz,
        item_show_type=item_show_type,
        query=query,
        exclude_keywords=exclude_keywords,
        since_ts=since_ts,
        until_ts=until_ts,
        sort_mode=sort_mode,
        limit=page_size,
        offset=offset,
        article_id=article_id,
    )
    rows = fetchall_rows(storage, query_sql, params, normalize=_normalize_record)
    for row in rows:
        row['item_show_type'] = _coalesce_item_show_type(row.get('item_show_type'))
        row["account_avatar_url"] = f"/api/account/{row['biz']}/avatar"
    has_active_filters = (
        article_id not in (None, '')
        or item_show_type is not None
        or bool(query)
        or since_ts is not None
        or until_ts is not None
        or bool(exclude_keywords)
    )
    if has_active_filters:
        total = _count_articles(
            storage=storage,
            group_id=group_id,
            biz=biz,
            item_show_type=item_show_type,
            query=query,
            exclude_keywords=exclude_keywords,
            since_ts=since_ts,
            until_ts=until_ts,
            article_id=article_id,
        )
    else:
        total = _get_cached_article_total(storage, group_id=group_id, biz=biz)
    item_show_type_facets = _count_article_item_show_type_facets(
        storage=storage,
        group_id=group_id,
        biz=biz,
        query=query,
        exclude_keywords=exclude_keywords,
        since_ts=since_ts,
        until_ts=until_ts,
        article_id=article_id,
    )
    return {
        'articles': rows,
        'page': page,
        'page_size': page_size,
        'total': total,
        'item_show_type_facets': item_show_type_facets,
    }


def _get_article(storage: PostgresStorage, article_id: int) -> dict[str, Any]:
    article = fetchone_row(
        storage,
        (
            "SELECT a.id, a.biz, a.article_id, a.title, a.item_show_type, a.author, a.digest, a.cover, a.link,"
            " a.source_url, a.publish_at, a.created_at,"
            " acc.nickname AS account_nickname, acc.alias AS account_alias,"
            " acc.round_head_img AS account_avatar, acc.group_id, g.name AS group_name"
            " FROM articles a"
            " JOIN accounts acc ON acc.biz = a.biz"
            " LEFT JOIN account_groups g ON g.id = acc.group_id"
            " WHERE a.id = %s"
        ),
        [article_id],
        normalize=_normalize_record,
    )
    if not article:
        raise ApiError("Article not found", status=404)
    article['item_show_type'] = _coalesce_item_show_type(article.get('item_show_type'))
    article["account_avatar_url"] = f"/api/account/{article['biz']}/avatar"

    content_row = fetchone_row(
        storage,
        "SELECT content_json, clean_html, updated_at FROM article_content WHERE article_pk = %s",
        [article_id],
        normalize=_normalize_record,
    )
    content_json = None
    clean_html = None
    content_updated_at: str | None = None
    decode_failed = False
    if content_row:
        content_json = content_row.get("content_json")
        clean_html = content_row.get("clean_html")
        content_updated_at = content_row.get('updated_at')
        if isinstance(content_json, str):
            try:
                content_json = json.loads(content_json)
            except json.JSONDecodeError:
                decode_failed = True
                content_json = None
    if content_row is None:
        content_status = 'missing'
    elif decode_failed:
        content_status = 'invalid'
    elif content_json is None:
        content_status = 'empty'
    elif isinstance(content_json, list):
        content_status = 'ok'
    else:
        content_status = 'invalid'

    images, blocked_image_ids = _get_visible_article_images(storage, article_id)
    if isinstance(content_json, list) and blocked_image_ids:
        content_json = _filter_blocked_content_blocks(content_json, blocked_image_ids)
    return {
        "article": article,
        "content": content_json,
        "content_status": content_status,
        "content_updated_at": content_updated_at,
        "images": images,
    }


def _list_article_images(storage: PostgresStorage, article_id: int) -> list[dict[str, Any]]:
    images, _blocked_image_ids = _get_visible_article_images(storage, article_id)
    return images


def _get_visible_article_images(
    storage: PostgresStorage,
    article_id: int,
) -> tuple[list[dict[str, Any]], set[int]]:
    images = storage.images.get_article_images(article_id)
    if storage.images.has_blocked_hashes():
        for image in images:
            if not image.get('content_hash'):
                try:
                    ensure_image_hash(storage, int(image['id']))
                except Exception:
                    logger.warning('Failed to ensure hash for image %s', image['id'])
    blocked_image_ids = storage.images.list_blocked_image_ids(article_id)
    visible_images = [image for image in images if int(image['id']) not in blocked_image_ids]
    return visible_images, blocked_image_ids


def _filter_blocked_content_blocks(
    content_blocks: list[dict[str, Any]],
    blocked_image_ids: set[int],
) -> list[dict[str, Any]]:
    return [
        block for block in content_blocks
        if not (block.get('type') == 'image' and block.get('image_id') in blocked_image_ids)
    ]


def _ensure_image_hash(storage: PostgresStorage, image_id: int, *, allow_origin_fetch: bool = True) -> dict[str, Any]:
    try:
        with storage.transaction():
            return ensure_image_hash(storage, image_id, allow_origin_fetch=allow_origin_fetch)
    except LookupError as exc:
        raise ApiError(str(exc), status=404) from exc
    except RuntimeError as exc:
        raise ApiError(str(exc), status=502) from exc


def _block_image(storage: PostgresStorage, image_id: int) -> dict[str, Any]:
    hash_record = _ensure_image_hash(storage, image_id)
    with storage.transaction():
        storage.images.block_image_hash(
            hash_algo=hash_record['hash_algo'],
            content_hash=hash_record['content_hash'],
            source_image_id=image_id,
        )
    return {
        'image_id': image_id,
        'hash_algo': hash_record['hash_algo'],
        'content_hash': hash_record['content_hash'],
        'blocked': True,
    }


def _fetch_image(storage: PostgresStorage, image_id: int) -> tuple[bytes, str]:
    try:
        return fetch_image_bytes(storage, image_id)
    except LookupError as exc:
        raise ApiError(str(exc), status=404) from exc
    except RuntimeError as exc:
        raise ApiError(str(exc), status=502) from exc


def _list_feed(
    storage: PostgresStorage,
    *,
    group_id: int | None,
    biz: str | None,
    query: str | None,
    since_ts: int | None,
    until_ts: int | None,
    limit: int,
) -> list[dict[str, Any]]:
    query_text = (query or '').strip()
    sort_mode = _normalize_article_sort(None, has_query=bool(query_text))
    query_sql, params = _build_article_query(
        storage=storage,
        group_id=group_id,
        biz=biz,
        item_show_type=None,
        query=query_text or None,
        exclude_keywords=None,
        since_ts=since_ts,
        until_ts=until_ts,
        sort_mode=sort_mode,
        limit=limit,
        offset=0,
    )
    return fetchall_rows(storage, query_sql, params, normalize=_normalize_record)


