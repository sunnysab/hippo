"""Mixed feed endpoint (JSON and RSS)."""

from datetime import datetime

from fastapi import APIRouter, Depends, Request, Response

from ...article_queries import _list_feed, _normalize_record, _parse_date
from ...config import DEFAULT_HOST, DEFAULT_PORT
from ...exceptions import ApiError
from ...models import User
from ...rss import build_rss_xml, query_rss_items
from ...storage import PostgresStorage, fetchall_rows
from ..deps import current_user_optional, get_storage, parse_group_ids

router = APIRouter()


@router.get('/feed/mixed', response_model=None)
async def list_feed(
    request: Request,
    group_id: int | None = None,
    group_ids: str | None = None,
    biz: str | None = None,
    q: str | None = None,
    limit: int = 50,
    format: str | None = None,
    since: str | None = None,
    until: str | None = None,
    days: int | None = None,
    token: str | None = None,
    storage: PostgresStorage = Depends(get_storage),
    user: User | None = Depends(current_user_optional),
):
    """
    获取所有公众号或特定分组的混合文章流。
    支持 JSON 和 RSS 输出。

    Args:
        request (Request): 请求对象。
        group_id (int | None): 按单个分组 ID 筛选 (向后兼容)。
        group_ids (str | None): 按多个分组 ID 筛选，逗号分隔 (如 "1,2,3")。
        biz (str | None): 按公众号 biz 筛选。
        q (str | None): 搜索关键词。
        limit (int): 返回条目数量 (默认: 50)。
        format (str | None): 输出格式 ("rss" 表示 RSS Feed)。
        since (str | None): 起始日期筛选。
        until (str | None): 结束日期筛选。
        days (int | None): 按最近天数筛选。
        token (str | None): RSS 阅读器无法携带 cookie，可用长期会话令牌代替登录。

    Returns:
        dict | Response: 文章列表或 RSS XML 响应。
    """
    # RSS readers cannot hold a cookie, so a long-lived session token is
    # accepted as a query parameter. Everything else uses the normal session.
    resolved_user = user
    if resolved_user is None and token:
        resolved_user = await storage.sessions.resolve(token)
    if resolved_user is None:
        raise ApiError('未登录', status=401)
    user_id = resolved_user.id

    output_format = (format or '').lower()
    since_ts = _parse_date(since)
    until_ts = _parse_date(until, end_of_day=True)
    if days:
        now = datetime.utcnow()
        since_ts = int(now.timestamp() - days * 86400)
    parsed_group_ids = parse_group_ids(group_ids, group_id)
    if output_format == 'rss':
        group_names: list[str] = []
        if parsed_group_ids:
            rows = await fetchall_rows(
                storage,
                'SELECT name FROM account_groups WHERE id = ANY(%s)',
                [parsed_group_ids],
                normalize=_normalize_record,
            )
            group_names = [row.get('name') or '' for row in rows]
            if not group_names:
                raise ApiError('Group not found', status=404)
        host = request.headers.get('host') or f'{DEFAULT_HOST}:{DEFAULT_PORT}'
        scheme = request.url.scheme or 'http'
        image_base = f'{scheme}://{host}'
        items = await query_rss_items(
            user_id=user_id,
            group_names=group_names,
            limit=min(max(limit, 1), 500),
            days=days,
            since=since,
            until=until,
            image_base_url=image_base,
        )
        title = 'Hippo RSS'
        description = 'Hippo RSS feed'
        if group_names:
            title = f'{group_names[0]} - Hippo RSS'
            description = f'RSS feed for {group_names[0]}'
        xml = build_rss_xml(
            title=title,
            link=image_base,
            description=description,
            items=items,
        )
        return Response(
            content=xml.encode('utf-8'),
            media_type='application/rss+xml; charset=utf-8',
        )
    payload = await _list_feed(
        storage,
        user_id=user_id,
        group_ids=parsed_group_ids,
        biz=biz or None,
        query=q or None,
        since_ts=since_ts,
        until_ts=until_ts,
        limit=min(max(limit, 1), 500),
    )
    return {'articles': payload}
