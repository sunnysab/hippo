"""Article query building, normalization, and data-access helpers."""

from __future__ import annotations

import json
import re
from typing import Any

try:
    import jieba
except Exception:  # pragma: no cover - optional fallback
    jieba = None

from .exceptions import ApiError
from .image_hashes import IMAGE_HASH_ALGO, compute_image_content_hash, ensure_image_hash, fetch_image_bytes
from .logger import get_logger
from .storage import PostgresStorage, fetchall_rows, fetchone_row
from .utils import normalize_value, parse_iso_date_to_timestamp

logger = get_logger(__name__)


ARTICLE_SORT_PUBLISH_AT_DESC = 'publish_at_desc'
ARTICLE_SORT_RELEVANCE_DESC = 'relevance_desc'
_ARTICLE_SORT_VALUES = {ARTICLE_SORT_PUBLISH_AT_DESC, ARTICLE_SORT_RELEVANCE_DESC}
_ITEM_SHOW_TYPE_VALUES = {0, 5, 6, 7, 8, 10, 11, 17}
_ARTICLE_EXCLUDE_KEYWORD_LIMIT = 20


def _build_item_show_type_where_clause(item_show_type: int) -> tuple[str, list[int]]:
    if item_show_type == 0:
        return '(a.item_show_type = %s OR a.item_show_type IS NULL)', [0]
    return 'a.item_show_type = %s', [item_show_type]


#: WeChat's layout type, flattened into something platform-neutral. The raw type
#: stays available for the facet filter, which is per-platform by nature.
CONTENT_KIND_BY_ITEM_SHOW_TYPE: dict[int, str] = {
    0: 'article',
    5: 'video',
    6: 'audio',
    7: 'audio',
    8: 'gallery',
    10: 'text',
    11: 'article',
    17: 'short',
}

#: Sources whose items have one obvious kind. A podcast feed is audio; a video
#: platform is video; anything unregistered degrades to a plain article, which is
#: the reading experience that always works.
CONTENT_KIND_BY_SOURCE: dict[str, str] = {
    'bilibili': 'video',
    'youtube': 'video',
    'podcast': 'audio',
}


def content_kind(source: Any, item_show_type: Any) -> str:
    """What kind of thing this is, whichever platform it came from."""
    platform = str(source or '').strip()
    if platform and platform != 'weixin':
        return CONTENT_KIND_BY_SOURCE.get(platform, 'article')
    return CONTENT_KIND_BY_ITEM_SHOW_TYPE.get(_coalesce_item_show_type(item_show_type) or 0, 'article')


def _coalesce_item_show_type(value: Any) -> int | None:
    if value is None:
        return 0
    try:
        return int(value)
    except TypeError, ValueError:
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


def _normalize_article_sort(value: str | None, *, has_query: bool) -> str:
    if value in (None, ''):
        return ARTICLE_SORT_RELEVANCE_DESC if has_query else ARTICLE_SORT_PUBLISH_AT_DESC
    sort = value.strip().lower()
    if sort not in _ARTICLE_SORT_VALUES:
        raise ApiError('Invalid sort', status=400)
    if sort == ARTICLE_SORT_RELEVANCE_DESC and not has_query:
        return ARTICLE_SORT_PUBLISH_AT_DESC
    return sort


def _build_article_search_tsquery(query: str | None) -> tuple[str, list[str]]:
    query_text = query.strip() if query else ''
    if not query_text:
        return '', []
    return "plainto_tsquery('jiebaqry', %s)", [query_text]


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
            '('
            "LOWER(COALESCE(a.title, '')) LIKE %s"
            " OR LOWER(COALESCE(a.digest, '')) LIKE %s"
            " OR LOWER(COALESCE(a.author, '')) LIKE %s"
            ')'
        )
        params.extend([pattern, pattern, pattern])
    return f'NOT ({" OR ".join(clauses)})', params


def _parse_date(value: str | None, *, end_of_day: bool = False) -> int | None:
    try:
        return parse_iso_date_to_timestamp(value, end_of_day=end_of_day)
    except ValueError as exc:
        raise ApiError(f'Invalid date: {value}') from exc


def _normalize_record(record: dict[str, Any]) -> dict[str, Any]:
    return {key: normalize_value(value) for key, value in record.items()}


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
    chunks = re.findall(r'[\u4e00-\u9fff]+|[A-Za-z0-9_]+', trimmed)
    tokens: list[str] = []
    for chunk in chunks:
        if re.fullmatch(r'[\u4e00-\u9fff]+', chunk):
            tokens.extend(list(chunk))
        else:
            tokens.append(chunk)
    return tokens[:12]


#: Restricts a query to the accounts a user follows. Aliases the row as ``sub``
#: so callers can filter on ``sub.group_id``. Its ``user_id`` parameter is bound
#: before every WHERE parameter because the join appears first in the SQL.
SUBSCRIPTION_JOIN = ' JOIN subscription sub ON sub.biz = a.biz AND sub.user_id = %s AND NOT sub.is_disabled'

#: Read state is per reader, so it hangs off a side table joined by (user, article).
#: It follows the subscription join, so its ``user_id`` is bound second.
READ_STATE_JOIN = ' LEFT JOIN article_read ar ON ar.article_pk = a.id AND ar.user_id = %s'

#: Stars are per reader, like read state, and follow the read join.
STAR_JOIN = ' LEFT JOIN article_star st ON st.article_pk = a.id AND st.user_id = %s'

#: The source predicate reads ``acc.source``, so callers that filter by source
#: must join accounts. The join itself binds no parameter.
ACCOUNTS_JOIN = ' JOIN accounts acc ON acc.biz = a.biz'

#: The list thumbnail: one image id per article, so the card can point at
#: ``/api/image/{id}`` instead of a third-party CDN URL.
#:
#: A published cover counts even before its bytes are archived — the image route
#: fetches those on demand — otherwise every article whose cover was registered
#: but not downloaded yet came back coverless. An unarchived *body* image is
#: still not a thumbnail: WeChat ships dozens per article, and a list request
#: must not turn into a download of all of them. Whatever its bytes, a candidate
#: whose known hash is globally blocked is never handed out. Cover first, then
#: whatever is already archived, then reading order.
#: (``blocked_image_hashes`` matching is NULL-safe by design: an image whose hash
#: is not known yet cannot be matched here and is decided by the image route.)
ARTICLE_THUMB_IMAGE_SQL = (
    'LEFT JOIN LATERAL ('
    '  SELECT id FROM article_images i'
    '  WHERE i.article_pk = a.id'
    "    AND ((i.s3_key IS NOT NULL AND i.s3_key <> '')"
    "         OR (i.kind = 'cover' AND i.orig_url IS NOT NULL AND i.orig_url <> ''))"
    '    AND NOT EXISTS ('
    '      SELECT 1 FROM blocked_image_hashes b'
    '      WHERE b.hash_algo = i.hash_algo AND b.content_hash = i.content_hash'
    '    )'
    "  ORDER BY (i.kind = 'cover') DESC, (i.s3_key IS NOT NULL AND i.s3_key <> '') DESC, i.position ASC"
    '  LIMIT 1'
    ') img ON TRUE'
)


#: Sources are an open set — the column is a discriminator, not an enum — so the
#: filter is parsed, not validated against a registry.
MAX_SOURCE_FILTER_LENGTH = 64


def _normalize_source_filter(value: str | None) -> list[str] | None:
    """`source=weixin,bilibili` becomes a list. Blank entries are skipped, so a
    trailing comma or an empty parameter simply means "no filter"."""
    if not value:
        return None
    if len(value) > MAX_SOURCE_FILTER_LENGTH:
        raise ApiError('Invalid source filter', status=400)
    sources: list[str] = []
    for raw in value.split(','):
        source = raw.strip()
        if not source:
            continue
        if not source.isascii():
            raise ApiError('Invalid source filter', status=400)
        if source not in sources:
            sources.append(source)
    return sources or None


def _normalize_read_filter(value: str | None) -> bool:
    """``read=unread`` is the only read filter today; anything else is a bug."""
    if value in (None, ''):
        return False
    if value == 'unread':
        return True
    raise ApiError('Invalid read filter', status=400)


def _normalize_starred_filter(value: str | None) -> bool:
    """``starred=true`` (or ``1``) keeps only starred articles."""
    if value in (None, ''):
        return False
    if value in ('true', '1'):
        return True
    raise ApiError('Invalid starred filter', status=400)


def _build_article_where_clause(
    *,
    group_ids: list[int] | None,
    biz: str | None,
    item_show_type: int | None,
    query: str | None,
    exclude_keywords: list[str] | None,
    since_ts: int | None,
    until_ts: int | None,
    article_id: str | None = None,
    unread_only: bool = False,
    starred_only: bool = False,
    sources: list[str] | None = None,
) -> tuple[str, list[Any], str, list[str]]:
    where: list[str] = []
    params: list[Any] = []
    query_text = query.strip() if query else ''
    query_tsquery_sql, query_tsquery_params = _build_article_search_tsquery(query_text)

    if article_id:
        where.append('a.article_id = %s')
        params.append(article_id)
    if group_ids is not None:
        where.append('sub.group_id = ANY(%s)')
        params.append(group_ids)
    if biz:
        where.append('a.biz = %s')
        params.append(biz)
    if sources:
        where.append('acc.source = ANY(%s)')
        params.append(sources)
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
    if unread_only:
        where.append('ar.read_at IS NULL')
    if starred_only:
        where.append('st.starred_at IS NOT NULL')

    where_sql = f'WHERE {" AND ".join(where)}' if where else ''
    return where_sql, params, query_tsquery_sql, query_tsquery_params


def _build_article_query(
    *,
    storage: PostgresStorage,
    user_id: int,
    group_ids: list[int] | None,
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
    unread_only: bool = False,
    starred_only: bool = False,
    sources: list[str] | None = None,
) -> tuple[str, list[Any]]:
    select_params: list[Any] = []
    rank_select = ''
    order_sql = 'ORDER BY a.publish_at DESC NULLS LAST, a.id DESC'

    where_sql, params, query_tsquery_sql, query_tsquery_params = _build_article_where_clause(
        group_ids=group_ids,
        biz=biz,
        item_show_type=item_show_type,
        query=query,
        exclude_keywords=exclude_keywords,
        since_ts=since_ts,
        until_ts=until_ts,
        article_id=article_id,
        unread_only=unread_only,
        starred_only=starred_only,
        sources=sources,
    )

    if sort_mode == ARTICLE_SORT_RELEVANCE_DESC and query_tsquery_sql:
        rank_select = f', ts_rank(a.search_vector, {query_tsquery_sql}) AS rank'
        select_params.extend(query_tsquery_params)
        order_sql = 'ORDER BY rank DESC, a.publish_at DESC NULLS LAST, a.id DESC'

    limit_sql = 'LIMIT %s OFFSET %s'

    image_select = 'img.id AS image_id'

    query_sql = (
        'SELECT a.id, a.biz, a.article_id, a.title, a.item_show_type, a.author, a.digest, a.cover, a.link,'
        ' a.source_url, a.publish_at, a.duration_seconds, a.created_at, ar.read_at, st.starred_at, acc.source,'
        ' acc.nickname AS account_nickname, acc.alias AS account_alias,'
        ' acc.round_head_img AS account_avatar,'
        ' sub.group_id, g.name AS group_name,'
        f' {image_select}'
        f'{rank_select}'
        ' FROM articles a'
        f'{ACCOUNTS_JOIN}'
        f'{SUBSCRIPTION_JOIN}'
        f'{READ_STATE_JOIN}'
        f'{STAR_JOIN}'
        ' LEFT JOIN account_groups g ON g.id = sub.group_id'
        f' {ARTICLE_THUMB_IMAGE_SQL}'
        f' {where_sql}'
        f' {order_sql}'
        f' {limit_sql}'
    )
    params = [*select_params, user_id, user_id, user_id, *params, limit, offset]
    return query_sql, params


async def _count_articles(
    *,
    storage: PostgresStorage,
    user_id: int,
    group_ids: list[int] | None,
    biz: str | None,
    item_show_type: int | None,
    query: str | None,
    exclude_keywords: list[str] | None,
    since_ts: int | None,
    until_ts: int | None,
    article_id: str | None = None,
    unread_only: bool = False,
    starred_only: bool = False,
    sources: list[str] | None = None,
) -> int:
    where_sql, params, _tsquery_sql, _tsquery_params = _build_article_where_clause(
        group_ids=group_ids,
        biz=biz,
        item_show_type=item_show_type,
        query=query,
        exclude_keywords=exclude_keywords,
        since_ts=since_ts,
        until_ts=until_ts,
        article_id=article_id,
        unread_only=unread_only,
        starred_only=starred_only,
        sources=sources,
    )
    # The extra joins are only worth their cost when a predicate uses them.
    state_join = f'{READ_STATE_JOIN if unread_only else ""}{STAR_JOIN if starred_only else ""}'
    state_params = [user_id] * ((1 if unread_only else 0) + (1 if starred_only else 0))
    accounts_join = ACCOUNTS_JOIN if sources else ''
    row = await fetchone_row(
        storage,
        (f'SELECT COUNT(*) AS total FROM articles a{accounts_join}{SUBSCRIPTION_JOIN}{state_join} {where_sql}'),
        [user_id, *state_params, *params],
        normalize=_normalize_record,
    )
    return int(row.get('total') or 0) if row else 0


async def _article_item_show_type_facets(
    *,
    storage: PostgresStorage,
    user_id: int,
    group_ids: list[int] | None,
    biz: str | None,
    query: str | None,
    exclude_keywords: list[str] | None,
    since_ts: int | None,
    until_ts: int | None,
    article_id: str | None = None,
    unread_only: bool = False,
    starred_only: bool = False,
    sources: list[str] | None = None,
) -> tuple[list[dict[str, int]], int]:
    """Distribution by layout type, plus the size of the whole set.

    The grand total rides along as a window function so the list does not have
    to count the same rows a second time — on its own that count costs about as
    much as this scan. Unknown types are dropped from the returned facets but
    still count towards the total, which is why the two cannot simply be summed.
    """
    where_sql, params, _tsquery_sql, _tsquery_params = _build_article_where_clause(
        group_ids=group_ids,
        biz=biz,
        item_show_type=None,
        query=query,
        exclude_keywords=exclude_keywords,
        since_ts=since_ts,
        until_ts=until_ts,
        article_id=article_id,
        unread_only=unread_only,
        starred_only=starred_only,
        sources=sources,
    )
    state_join = f'{READ_STATE_JOIN if unread_only else ""}{STAR_JOIN if starred_only else ""}'
    state_params = [user_id] * ((1 if unread_only else 0) + (1 if starred_only else 0))
    accounts_join = ACCOUNTS_JOIN if sources else ''
    rows = await fetchall_rows(
        storage,
        (
            'SELECT COALESCE(a.item_show_type, 0) AS item_show_type, COUNT(*) AS total,'
            ' SUM(COUNT(*)) OVER () AS grand_total'
            ' FROM articles a'
            f'{accounts_join}'
            f'{SUBSCRIPTION_JOIN}'
            f'{state_join}'
            f' {where_sql}'
            ' GROUP BY COALESCE(a.item_show_type, 0)'
            ' ORDER BY COALESCE(a.item_show_type, 0) ASC'
        ),
        [user_id, *state_params, *params],
        normalize=_normalize_record,
    )
    order_map = {value: index for index, value in enumerate(sorted(_ITEM_SHOW_TYPE_VALUES))}
    facet_counts: dict[int, int] = {}
    grand_total = 0
    for row in rows:
        grand_total = int(row.get('grand_total') or 0)
        item_show_type = row.get('item_show_type')
        total = row.get('total')
        if item_show_type is None:
            item_show_type = 0
        try:
            normalized_type = int(item_show_type)
            normalized_total = int(total)
        except TypeError, ValueError:
            continue
        if normalized_type not in _ITEM_SHOW_TYPE_VALUES or normalized_total <= 0:
            continue
        facet_counts[normalized_type] = facet_counts.get(normalized_type, 0) + normalized_total
    facets = [{'item_show_type': item_show_type, 'count': count} for item_show_type, count in facet_counts.items()]
    facets.sort(key=lambda item: order_map.get(item['item_show_type'], 999))
    return facets, grand_total


async def _list_articles(
    storage: PostgresStorage,
    *,
    user_id: int,
    group_ids: list[int] | None,
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
    unread_only: bool = False,
    starred_only: bool = False,
    sources: list[str] | None = None,
) -> dict[str, Any]:
    offset = max(page - 1, 0) * page_size
    query_sql, params = _build_article_query(
        storage=storage,
        user_id=user_id,
        group_ids=group_ids,
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
        unread_only=unread_only,
        starred_only=starred_only,
        sources=sources,
    )
    rows = await fetchall_rows(storage, query_sql, params, normalize=_normalize_record)
    for row in rows:
        row['item_show_type'] = _coalesce_item_show_type(row.get('item_show_type'))
        row['content_kind'] = content_kind(row.get('source'), row.get('item_show_type'))
        row['account_avatar_url'] = f'/api/account/{row["biz"]}/avatar'
    item_show_type_facets, facet_total = await _article_item_show_type_facets(
        storage=storage,
        user_id=user_id,
        group_ids=group_ids,
        biz=biz,
        query=query,
        exclude_keywords=exclude_keywords,
        since_ts=since_ts,
        until_ts=until_ts,
        article_id=article_id,
        unread_only=unread_only,
        starred_only=starred_only,
        sources=sources,
    )
    # The facets ignore their own filter, so their total is the answer unless a
    # layout type is selected — only then is a second count worth its cost.
    if item_show_type is None:
        total = facet_total
    else:
        total = await _count_articles(
            storage=storage,
            user_id=user_id,
            group_ids=group_ids,
            biz=biz,
            item_show_type=item_show_type,
            query=query,
            exclude_keywords=exclude_keywords,
            since_ts=since_ts,
            until_ts=until_ts,
            article_id=article_id,
            unread_only=unread_only,
            starred_only=starred_only,
            sources=sources,
        )
    return {
        'articles': rows,
        'page': page,
        'page_size': page_size,
        'total': total,
        'item_show_type_facets': item_show_type_facets,
    }


async def _get_article(storage: PostgresStorage, user_id: int, article_id: int) -> dict[str, Any]:
    article = await fetchone_row(
        storage,
        (
            'SELECT a.id, a.biz, a.article_id, a.title, a.item_show_type, a.author, a.digest, a.cover, a.link,'
            ' a.source_url, a.publish_at, a.duration_seconds, a.created_at, acc.source,'
            ' (SELECT st.starred_at FROM article_star st'
            '   WHERE st.article_pk = a.id AND st.user_id = %s) AS starred_at,'
            ' (SELECT ar.read_at FROM article_read ar'
            '   WHERE ar.article_pk = a.id AND ar.user_id = %s) AS read_at,'
            ' acc.nickname AS account_nickname, acc.alias AS account_alias,'
            ' acc.round_head_img AS account_avatar, sub.group_id, g.name AS group_name'
            ' FROM articles a'
            f'{ACCOUNTS_JOIN}'
            f'{SUBSCRIPTION_JOIN}'
            ' LEFT JOIN account_groups g ON g.id = sub.group_id'
            ' WHERE a.id = %s'
        ),
        [user_id, user_id, user_id, article_id],
        normalize=_normalize_record,
    )
    if not article:
        raise ApiError('Article not found', status=404)
    article['item_show_type'] = _coalesce_item_show_type(article.get('item_show_type'))
    article['content_kind'] = content_kind(article.get('source'), article.get('item_show_type'))
    article['account_avatar_url'] = f'/api/account/{article["biz"]}/avatar'

    content_row = await fetchone_row(
        storage,
        'SELECT content_json, updated_at FROM article_content WHERE article_pk = %s',
        [article_id],
        normalize=_normalize_record,
    )
    content_json = None
    content_updated_at: str | None = None
    decode_failed = False
    if content_row:
        content_json = content_row.get('content_json')
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

    images, blocked_image_ids = await _get_visible_article_images(storage, article_id)
    visible_image_ids = {int(image['id']) for image in images}
    if isinstance(content_json, list):
        if blocked_image_ids:
            content_json = _filter_blocked_content_blocks(content_json, blocked_image_ids)
        content_json = _proxy_content_posters(
            content_json,
            cover=article.get('cover'),
            visible_image_ids=visible_image_ids,
        )

    # A stored file plays natively even when the transcript never arrived, which is
    # the whole point of archiving before transcribing. A damaged transcript is not
    # a hole to fill: it keeps its honest status and no player is shown above it.
    media_url = None
    if content_status in ('missing', 'empty', 'ok'):
        media_url = await _stored_archived_video_url(storage, article, content_json)
    if media_url is not None:
        content_json = _attach_archived_video(
            content_json,
            url=media_url,
            poster=_canonical_cover_path(article.get('cover'), visible_image_ids),
        )
        if content_status in ('missing', 'empty'):
            # The response carries playable content, so the status says so; it
            # describes this payload, never the transcription job's progress.
            content_status = 'ok'
    elif content_status == 'missing':
        # 视频/播客的正文由转写补，别让读者以为「没同步」是坏了。
        media_state = (await storage.media_jobs.states_for_articles([article_id])).get(article_id)
        if media_state in ('pending', 'processing'):
            content_status = 'processing'

    fetch_diagnostic = None
    if content_status != 'ok':
        fetch_diagnostic = await fetchone_row(
            storage,
            """
            SELECT last_error, error_type, retryable, attempts, last_attempt_at
              FROM article_download_attempts
             WHERE biz = %s AND article_id = %s
            """,
            [article['biz'], article['article_id']],
            normalize=_normalize_record,
        )
    return {
        'article': article,
        'content': content_json,
        'content_status': content_status,
        'content_updated_at': content_updated_at,
        # Keep our diagnostic verbatim; it does not assert why WeChat omitted content.
        'content_fetch_diagnostic': fetch_diagnostic,
        'images': images,
    }


async def _assert_article_visible(storage: PostgresStorage, user_id: int, article_id: int) -> None:
    """Raise 404 unless the article belongs to an account the user follows.

    404 rather than 403: a caller must not be able to tell a foreign article
    apart from one that does not exist.
    """
    row = await fetchone_row(
        storage,
        f'SELECT 1 AS ok FROM articles a{SUBSCRIPTION_JOIN} WHERE a.id = %s',
        [user_id, article_id],
        normalize=_normalize_record,
    )
    if not row:
        raise ApiError('Article not found', status=404)


async def _list_article_images(
    storage: PostgresStorage,
    user_id: int,
    article_id: int,
) -> list[dict[str, Any]]:
    await _assert_article_visible(storage, user_id, article_id)
    images, _blocked_image_ids = await _get_visible_article_images(storage, article_id)
    return images


async def _get_visible_article_images(
    storage: PostgresStorage,
    article_id: int,
) -> tuple[list[dict[str, Any]], set[int]]:
    images = await storage.images.get_article_images(article_id)
    if await storage.images.has_blocked_hashes():
        for image in images:
            if not image.get('content_hash'):
                try:
                    await ensure_image_hash(storage, int(image['id']))
                except Exception:
                    logger.warning('Failed to ensure hash for image %s', image['id'])
    blocked_image_ids = await storage.images.list_blocked_image_ids(article_id)
    visible_images = [image for image in images if int(image['id']) not in blocked_image_ids]
    return visible_images, blocked_image_ids


def _filter_blocked_content_blocks(
    content_blocks: list[dict[str, Any]],
    blocked_image_ids: set[int],
) -> list[dict[str, Any]]:
    return [
        block
        for block in content_blocks
        if not (block.get('type') == 'image' and block.get('image_id') in blocked_image_ids)
    ]


def _cover_image_id(cover: Any) -> int | None:
    """``articles.cover`` holds an image id; legacy rows still hold a URL.

    Metadata is untrusted: ``'²'.isdigit()`` is true though ``int('²')`` raises,
    so only an ASCII decimal (and a positive one) is an id. Bad values are not
    an id — they must never turn a response into a 500.
    """
    if cover is None or isinstance(cover, bool):
        return None
    text = str(cover).strip()
    if not text.isascii() or not text.isdecimal():
        return None
    image_id = int(text)
    return image_id if image_id > 0 else None


#: Blocks whose poster is a URL the reader would otherwise fetch from the source.
_POSTER_BLOCK_TYPES = frozenset({'video', 'audio'})

#: Our own protected image route — the only poster we ever serve ourselves.
_HIPPO_IMAGE_PATH_RE = re.compile(r'^/api/image/([0-9]+)$')


def _canonical_cover_path(cover: Any, visible_image_ids: set[int]) -> str | None:
    """The protected route for this article's cover, or ``None`` when the reader
    has no cover to see. Both the poster mapping and the video hydration need the
    same answer, so the visibility rule lives in one place.
    """
    cover_id = _cover_image_id(cover)
    if cover_id is None or cover_id not in visible_image_ids:
        return None
    return f'/api/image/{cover_id}'


def _proxy_content_posters(
    blocks: list[dict[str, Any]],
    *,
    cover: Any,
    visible_image_ids: set[int],
) -> list[dict[str, Any]]:
    """Point every video/audio poster at the protected image route.

    A provider poster (Bilibili, YouTube) is the same cover metadata at another
    resolution or with a fresh signature, so it is not compared against the
    stored ``orig_url``: the article's canonical cover id is what the reader gets.
    Without a visible cover the external URL is dropped rather than kept as a
    fallback — the browser must not fetch an image we refused to serve, and a
    blocked cover is refused here even when some poster already points at our own
    route. A block with no poster is left alone: there is nothing to protect and
    no reason to invent one. The stored content is never edited; the same list is
    returned when nothing changed and copies only where a poster changed.
    """
    canonical = _canonical_cover_path(cover, visible_image_ids)
    changed = False
    updated: list[dict[str, Any]] = []
    for block in blocks:
        poster = block.get('cover_url') if block.get('type') in _POSTER_BLOCK_TYPES else None
        if not poster:
            updated.append(block)
            continue
        own = _HIPPO_IMAGE_PATH_RE.match(str(poster))
        replacement = canonical
        if own:
            # Already ours: keep it, unless the reader has blocked that image.
            replacement = poster if int(own.group(1)) in visible_image_ids else None
        if replacement == poster:
            updated.append(block)
            continue
        changed = True
        copied = dict(block)
        if replacement is None:
            copied.pop('cover_url', None)
        else:
            copied['cover_url'] = replacement
        updated.append(copied)
    return updated if changed else blocks


def _block_is_video(block: Any) -> bool:
    return isinstance(block, dict) and block.get('type') == 'video'


def _has_video_block(blocks: Any) -> bool:
    return isinstance(blocks, list) and any(_block_is_video(block) for block in blocks)


def _attach_archived_video(
    blocks: list[dict[str, Any]] | None,
    *,
    url: str,
    poster: str | None,
) -> list[dict[str, Any]]:
    """Give this article's own video a native player, without touching the stored JSON.

    An existing video block is rewritten where it sits — its ``embed_url`` removed
    so the reader gets our file rather than the provider's iframe, its provider
    metadata kept. An article whose transcript has no video block gets one in
    front, which is what a reader waiting on the transcript needs. The caller's
    list is never mutated: the stored ``content_json`` stays embed-only.
    """
    native: dict[str, Any] = {'type': 'video', 'url': url}
    if poster:
        native['cover_url'] = poster
    if blocks is None:
        return [native]
    replaced = False
    updated: list[dict[str, Any]] = []
    for block in blocks:
        if not _block_is_video(block):
            updated.append(block)
            continue
        copied = {key: value for key, value in block.items() if key != 'embed_url'}
        copied['url'] = url
        if poster:
            copied['cover_url'] = poster
        updated.append(copied)
        replaced = True
    if replaced:
        return updated
    return [native, *blocks]


async def _stored_archived_video_url(
    storage: PostgresStorage,
    article: dict[str, Any],
    content_json: Any,
) -> str | None:
    """``/api/media/{id}/video`` when this article's video is already in the store.

    Only asked for a video entry or an article that already carries a video block:
    a plain text article does not pay a media lookup on every read. The original
    URL is never the fallback here — a redirect to a third-party page is not a
    native player, and the front end keeps its old block when we hold no bytes.
    """
    if article.get('content_kind') != 'video' and not _has_video_block(content_json):
        return None
    article_pk = int(article['id'])
    asset = await storage.media_assets.get(article_pk, kind='video')
    if not asset or asset.get('state') != 'stored' or not str(asset.get('s3_key') or '').strip():
        return None
    return f'/api/media/{article_pk}/video'


async def _is_blocked_content_hash(storage: PostgresStorage, hash_algo: str, content_hash: str) -> bool:
    """Global block list membership, by content hash rather than by image id."""
    row = await fetchone_row(
        storage,
        'SELECT 1 AS blocked FROM blocked_image_hashes WHERE hash_algo = %s AND content_hash = %s LIMIT 1',
        [hash_algo, content_hash],
    )
    return row is not None


async def _ensure_image_hash(
    storage: PostgresStorage, image_id: int, *, allow_origin_fetch: bool = True
) -> dict[str, Any]:
    try:
        async with storage.transaction():
            return await ensure_image_hash(storage, image_id, allow_origin_fetch=allow_origin_fetch)
    except LookupError as exc:
        raise ApiError(str(exc), status=404) from exc
    except RuntimeError as exc:
        raise ApiError(str(exc), status=502, code='image.unavailable') from exc


async def _ensure_image_visible(storage: PostgresStorage, user_id: int, image_id: int) -> None:
    """404 unless the image belongs to an article this user subscribes to.

    Images are addressed by a global id, so without this check any signed-in
    user could walk the ids and read (or block) images from other people's
    subscriptions. The subscription has to be enabled, the same rule the article
    and media routes apply — a disabled feed is not a grant to its images.
    """
    row = await fetchone_row(
        storage,
        (f'SELECT 1 FROM article_images i JOIN articles a ON a.id = i.article_pk{SUBSCRIPTION_JOIN} WHERE i.id = %s'),
        [user_id, image_id],
    )
    if not row:
        raise ApiError('Image not found', status=404)


async def _block_image(storage: PostgresStorage, user_id: int, image_id: int) -> dict[str, Any]:
    await _ensure_image_visible(storage, user_id, image_id)
    hash_record = await _ensure_image_hash(storage, image_id)
    async with storage.transaction():
        await storage.images.block_image_hash(
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


async def _fetch_image(storage: PostgresStorage, user_id: int, image_id: int) -> tuple[bytes, str]:
    # Visibility first: an image from someone else's subscription is a 404 before
    # we even look at the block list.
    await _ensure_image_visible(storage, user_id, image_id)
    hash_row = await fetchone_row(
        storage,
        'SELECT hash_algo, content_hash FROM article_images WHERE id = %s',
        [image_id],
    )
    hash_algo = str(hash_row.get('hash_algo') or '') if hash_row else ''
    content_hash = str(hash_row.get('content_hash') or '') if hash_row else ''
    if hash_algo and content_hash and await _is_blocked_content_hash(storage, hash_algo, content_hash):
        # A blocked image is indistinguishable from one that does not exist.
        raise ApiError('Image not found', status=404)
    try:
        payload, content_type = await fetch_image_bytes(storage, image_id)
    except LookupError as exc:
        raise ApiError(str(exc), status=404) from exc
    except RuntimeError as exc:
        raise ApiError(str(exc), status=502, code='image.unavailable') from exc
    # Unknown hash: decide from the bytes we are already sending, so a blocked
    # image is never written out and the picture is not fetched twice.
    if not (hash_algo and content_hash) and await _is_blocked_content_hash(
        storage, IMAGE_HASH_ALGO, compute_image_content_hash(payload)
    ):
        raise ApiError('Image not found', status=404)
    return payload, content_type


async def _list_feed(
    storage: PostgresStorage,
    *,
    user_id: int,
    group_ids: list[int] | None,
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
        user_id=user_id,
        group_ids=group_ids,
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
    return await fetchall_rows(storage, query_sql, params, normalize=_normalize_record)
