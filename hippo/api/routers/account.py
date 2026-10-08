"""Group and account endpoints."""

from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Body, Depends, Response, status

from ...article_queries import _normalize_record, _tokenize_query
from ...avatar import _fetch_and_cache_avatar, _get_avatar_row, _upsert_avatar_url
from ...config import DEFAULT_GROUP_NAME
from ...exceptions import ApiError
from ...logger import get_logger
from ...models import AccountCredential, User
from ...storage import PostgresStorage, ensure_default_group, fetchone_row
from ...weixin_source import WeixinSource
from ..deps import current_user, get_storage, parse_group_ids
from ..responses import binary_response

logger = get_logger(__name__)
router = APIRouter(dependencies=[Depends(current_user)])


async def _list_groups(storage: PostgresStorage, user_id: int) -> list[dict[str, Any]]:
    return [g.model_dump() for g in await storage.groups.list_groups(user_id=user_id)]


async def _get_group(storage: PostgresStorage, user_id: int, group_id: int) -> dict[str, Any]:
    try:
        group = await storage.groups.get_group(group_id, user_id=user_id)
    except LookupError:
        raise ApiError('Group not found', status=404)
    return group.model_dump()


async def _update_group(
    storage: PostgresStorage,
    user_id: int,
    group_id: int,
    updates: dict[str, Any],
) -> dict[str, Any]:
    if not updates:
        raise ApiError('No fields to update')
    try:
        async with storage.transaction():
            group = await storage.groups.update_group(group_id, user_id=user_id, **updates)
    except LookupError:
        raise ApiError('Group not found', status=404)
    except ValueError as exc:
        raise ApiError(str(exc))
    return group.model_dump()


async def _delete_group(storage: PostgresStorage, user_id: int, group_id: int) -> None:
    default_group = await ensure_default_group(storage, user_id, name=DEFAULT_GROUP_NAME)
    default_id = default_group.id
    try:
        async with storage.transaction():
            await storage.groups.delete_group(group_id, default_id, user_id=user_id)
    except LookupError:
        raise ApiError('Group not found', status=404)
    except ValueError as exc:
        raise ApiError(str(exc), status=400)


async def _list_accounts(
    storage: PostgresStorage,
    user_id: int,
    *,
    group_ids: list[int] | None,
    query: str | None,
    page: int,
    page_size: int,
) -> dict[str, Any]:
    search_tokens: list[str] | None = None
    if query:
        tokens = _tokenize_query(query)
        if tokens:
            search_tokens = tokens
    return await storage.accounts.list_accounts_paginated(
        user_id=user_id,
        group_ids=group_ids,
        search_tokens=search_tokens,
        page=page,
        page_size=page_size,
    )


async def _get_account(storage: PostgresStorage, user_id: int, biz: str) -> dict[str, Any]:
    try:
        return _normalize_account_payload(await storage.accounts.get_account_detail(biz, user_id=user_id))
    except LookupError:
        raise ApiError('Account not found', status=404)


def _normalize_account_payload(account: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(account)
    normalized['alias'] = normalized.get('alias') or ''
    return normalized


#: Fields stored on the shared account row. Everything else is per-user.
_SHARED_ACCOUNT_FIELDS = ('nickname', 'alias', 'round_head_img')


async def _update_account(
    storage: PostgresStorage,
    user_id: int,
    biz: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Split the payload between the shared row and the caller's subscription.

    The catalogue (nickname, alias, avatar) is global; the grouping, the
    disabled flag and the sync interval belong to whoever is asking.
    """
    shared: dict[str, Any] = {key: payload[key] for key in _SHARED_ACCOUNT_FIELDS if key in payload}
    per_user: dict[str, Any] = {}
    if 'group_id' in payload:
        per_user['group_id'] = int(payload['group_id']) if payload['group_id'] is not None else None
    if 'is_disabled' in payload:
        per_user['is_disabled'] = bool(payload['is_disabled'])
    if 'sync_interval_days' in payload:
        value = payload['sync_interval_days']
        per_user['sync_interval_days'] = max(int(value), 1) if value is not None else None

    if not shared and not per_user:
        raise ApiError('No fields to update')

    async with storage.transaction():
        if shared:
            try:
                await storage.accounts.update_account_fields(biz, **shared)
            except LookupError:
                raise ApiError('Account not found', status=404)
        if per_user:
            followed = await storage.subscriptions.list_for_user(user_id)
            if not any(item['biz'] == biz for item in followed):
                raise ApiError('Account not found', status=404)
            if 'group_id' in per_user:
                await storage.subscriptions.set_group(user_id, [biz], per_user['group_id'])
            if 'is_disabled' in per_user:
                await storage.subscriptions.set_disabled(user_id, [biz], per_user['is_disabled'])
            if 'sync_interval_days' in per_user:
                await storage.subscriptions.set_interval(user_id, [biz], per_user['sync_interval_days'])
    return await _get_account(storage, user_id, biz)


@router.get('/group')
async def list_groups(
    storage: PostgresStorage = Depends(get_storage),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    """
    获取所有分组列表。

    Returns:
        dict: 包含默认分组 ID 和所有分组列表的字典。
    """
    default_group = await ensure_default_group(storage, user.id, name=DEFAULT_GROUP_NAME)
    return {
        'default_group_id': default_group.id,
        'groups': await _list_groups(storage, user.id),
    }


@router.post('/group', status_code=status.HTTP_201_CREATED)
async def create_group(
    body: dict[str, Any] = Body(default={}),
    storage: PostgresStorage = Depends(get_storage),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    """
    创建一个新的分组。

    Args:
        body (dict): 请求体，包含分组名称 "name"。

    Returns:
        dict: 创建的分组 ID 和名称。

    Raises:
        ApiError: 如果分组名称缺失或为空。
    """
    name = str(body.get('name', '')).strip()
    if not name:
        raise ApiError('Group name is required')
    async with storage.transaction():
        group = await storage.groups.upsert_group(name, user_id=user.id)
    return {'id': group.id, 'name': group.name}


@router.get('/group/{group_id}')
async def get_group(
    group_id: int,
    storage: PostgresStorage = Depends(get_storage),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    """
    获取指定分组的详细信息。

    Args:
        group_id (int): 分组 ID。

    Returns:
        dict: 分组详情，包括 ID、名称、同步设置和公众号数量。

    Raises:
        ApiError: 如果分组不存在。
    """
    return await _get_group(storage, user.id, group_id)


@router.patch('/group/{group_id}')
async def update_group(
    group_id: int,
    body: dict[str, Any] = Body(default={}),
    storage: PostgresStorage = Depends(get_storage),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    """
    更新指定分组的信息。

    Args:
        group_id (int): 分组 ID。
        body (dict): 需要更新的字段 (name, sync_mode, sync_recent_days)。

    Returns:
        dict: 更新后的分组详情。
    """
    updates: dict[str, Any] = {}
    if 'name' in body:
        name = str(body.get('name', '')).strip()
        if not name:
            raise ApiError('Group name is required')
        updates['name'] = name
    return await _update_group(storage, user.id, group_id, updates)


@router.delete('/group/{group_id}', status_code=status.HTTP_204_NO_CONTENT)
async def delete_group(
    group_id: int,
    storage: PostgresStorage = Depends(get_storage),
    user: User = Depends(current_user),
) -> Response:
    """
    删除指定分组。该分组下的公众号将被移动到默认分组。

    Args:
        group_id (int): 分组 ID。

    Returns:
        Response: HTTP 204 No Content。

    Raises:
        ApiError: 如果是默认分组或分组不存在。
    """
    await _delete_group(storage, user.id, group_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get('/account/search')
async def search_account(
    q: str = '',
    page: int = 1,
    page_size: int = 10,
    begin: int | None = None,
    storage: PostgresStorage = Depends(get_storage),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    """搜索公众号（数据源：weixin-rs daemon 的 H5 搜索）。

    返回的 ``biz`` 是 ``gh_…``：daemon 只认这个形状，真正的 fakeid
    （``accounts.biz``，``Mz…==``）由 ``POST /account`` 抓一篇文章回填。

    Args:
        q (str): 搜索关键词（公众号名称或微信号）。
        page (int): 页码（默认 1）。
        page_size (int): 每页数量（默认 10，最多 20）。
        begin (int | None): 可选的偏移量，优先于 page。

    Returns:
        dict: ``{results, page, page_size, total}``，字段与旧实现保持一致。
    """
    keyword = (q or '').strip()
    if not keyword:
        raise ApiError('q is required')
    limit = min(max(page_size, 1), 20)
    offset = begin if begin is not None else (max(page, 1) - 1) * limit
    try:
        async with WeixinSource() as source:
            items = await source.search_public_accounts(keyword, offset)
    except Exception as exc:
        raise ApiError(f'搜索公众号失败：{exc}', status=502) from exc

    accounts = await storage.accounts.list_followed_accounts(user.id)
    followed_biz = {account.biz for account in accounts}
    by_alias = {account.alias.lower(): account for account in accounts if account.alias}
    by_nickname = {account.nickname: account for account in accounts}

    results: list[dict[str, Any]] = []
    for item in items[:limit]:
        gh_id = str(item.get('userName') or '').strip()
        if not gh_id:
            continue
        nickname = str(item.get('nickName') or '').strip()
        alias = str(item.get('alias') or '').strip()
        # 微信 H5 搜索返回的是 headImgUrl / headHDImgUrl（曾经的 roundHeadImg/headImg 从未存在过）
        avatar_url = str(item.get('headImgUrl') or item.get('headHDImgUrl') or '').strip()
        known = by_alias.get(alias.lower()) if alias else None
        if known is None and nickname:
            known = by_nickname.get(nickname)
        # 以 gh_id 为键缓存：搜索头像接口拿到的就是 gh_id，而 accounts.biz 是 fakeid。
        if avatar_url:
            await _upsert_avatar_url(storage, gh_id, avatar_url)
        results.append(
            {
                'biz': gh_id,
                'nickname': nickname,
                'alias': alias,
                'round_head_img': avatar_url,
                'is_added': known is not None and known.biz in followed_biz,
                'avatar_url': f'/api/account/search/{quote(gh_id, safe="")}/avatar' if avatar_url else '',
            }
        )
    return {
        'results': results,
        'page': max(page, 1),
        'page_size': limit,
        'total': len(results),
    }


@router.get('/account/search/{biz}/avatar')
async def get_search_avatar(
    biz: str,
    storage: PostgresStorage = Depends(get_storage),
) -> Response:
    """
    获取搜索结果的公众号头像。

    头像按搜索结果里的 `gh_id` 缓存（搜索接口返回的 `avatar_url` 就是它），
    与已关注账号的 `accounts.biz`（fakeid）不是同一套键。

    Args:
        biz (str): 搜索结果的 gh_id。

    Returns:
        Response: 包含正确 Content-Type 的图片数据。
    """
    avatar = await _get_avatar_row(storage, biz)
    if not avatar:
        raise ApiError('Avatar not found', status=404)
    data = avatar.get('data')
    if not data:
        url = avatar.get('avatar_url')
        if url:
            cached = await _fetch_and_cache_avatar(storage, biz, url)
            if cached:
                payload, content_type = cached
                return binary_response(payload, content_type)
        raise ApiError('Avatar not found', status=404)
    payload = data.tobytes() if isinstance(data, memoryview) else bytes(data)
    content_type = avatar.get('content_type') or 'application/octet-stream'
    return binary_response(payload, content_type)


@router.get('/account')
async def list_accounts(
    group_id: int | None = None,
    group_ids: str | None = None,
    q: str | None = None,
    page: int = 1,
    page_size: int = 20,
    storage: PostgresStorage = Depends(get_storage),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    """
    列出已保存的公众号，支持筛选。

    Args:
        group_id (int | None): 按单个分组 ID 筛选 (向后兼容)。
        group_ids (str | None): 按多个分组 ID 筛选，逗号分隔 (如 "1,2,3")。
        q (str | None): 按关键词搜索 (昵称、微信号或 biz)。
        page (int): 页码。
        page_size (int): 每页数量。

    Returns:
        dict: 公众号列表和分页信息。
    """
    payload = await _list_accounts(
        storage,
        user.id,
        group_ids=parse_group_ids(group_ids, group_id),
        query=q or None,
        page=max(page, 1),
        page_size=min(max(page_size, 1), 200),
    )
    payload['accounts'] = [_normalize_account_payload(account) for account in payload.get('accounts', [])]
    return payload


@router.post('/account', status_code=status.HTTP_201_CREATED)
async def create_account(
    body: dict[str, Any] = Body(default={}),
    storage: PostgresStorage = Depends(get_storage),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    """
    添加一个新的公众号到数据库。

    Args:
        body (dict): 公众号详情，包括 biz, nickname 等。

    Returns:
        dict: 创建的公众号详情。

    Raises:
        ApiError: 如果缺少必填字段 (biz, nickname)。
    """
    required = ['biz', 'nickname']
    for field in required:
        if not body.get(field):
            raise ApiError(f'{field} is required')
    raw_biz = str(body['biz']).strip()
    biz = raw_biz
    # 搜索接口只能给 gh_（daemon 的形状），而 accounts.biz 是 fakeid（``Mz…==``），
    # 所以这里抓一页该号的文章、从 URL 的 __biz 回填；同时把 gh_ 存下来供后续列表用。
    gh_id: str | None = None
    if raw_biz.startswith('gh_'):
        gh_id = raw_biz
        try:
            async with WeixinSource() as source:
                biz = await source.resolve_fakeid(gh_id)
        except Exception as exc:
            raise ApiError(f'解析公众号 ID 失败：{exc}', status=502) from exc
    group_id = body.get('group_id')
    if group_id is None:
        default_group = await ensure_default_group(storage, user.id, name=DEFAULT_GROUP_NAME)
        group_id = default_group.id
    async with storage.transaction():
        account = await storage.accounts.upsert_account(
            AccountCredential(
                biz=biz,
                nickname=str(body['nickname']),
                alias=body.get('alias'),
                gh_id=gh_id,
                round_head_img=body.get('round_head_img'),
            )
        )
        # Adding an account to the catalogue also subscribes the caller to it.
        await storage.subscriptions.upsert(user.id, account.biz, group_id=int(group_id))
    return _normalize_account_payload(
        {
            'biz': account.biz,
            'nickname': account.nickname,
            'alias': account.alias,
            'round_head_img': account.round_head_img,
            'group_id': account.group_id,
        }
    )


@router.post('/account/move')
async def move_accounts(
    body: dict[str, Any] = Body(default={}),
    storage: PostgresStorage = Depends(get_storage),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    """
    批量移动公众号到另一个分组。

    Args:
        body (dict): 包含 'biz_list' (字符串列表) 和 'group_id' (整数)。

    Returns:
        dict: 更新的公众号数量。
    """
    biz_list = body.get('biz_list') or []
    if not isinstance(biz_list, list) or not biz_list:
        raise ApiError('biz_list is required')
    group_id = body.get('group_id')
    if group_id is None:
        default_group = await ensure_default_group(storage, user.id, name=DEFAULT_GROUP_NAME)
        group_id = default_group.id
    async with storage.transaction():
        updated = await storage.subscriptions.set_group(user.id, biz_list, group_id)
    return {'updated': updated}


@router.post('/account/batch')
async def batch_update_accounts(
    body: dict[str, Any] = Body(default={}),
    storage: PostgresStorage = Depends(get_storage),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    """
    批量更新多个公众号的同步设置。

    Args:
        body (dict): 包含 'biz_list' 和需更新的字段 ('sync_mode', 'sync_recent_days')。

    Returns:
        dict: 更新的公众号数量。
    """
    biz_list = body.get('biz_list') or []
    if not isinstance(biz_list, list) or not biz_list:
        raise ApiError('biz_list is required')
    updates: dict[str, Any] = {}
    if 'sync_interval_days' in body:
        value = body['sync_interval_days']
        updates['sync_interval_days'] = max(int(value), 1) if value is not None else None
    if not updates:
        raise ApiError('No fields to update')
    async with storage.transaction():
        updated = await storage.subscriptions.set_interval(user.id, biz_list, updates['sync_interval_days'])
    return {'updated': updated}


@router.get('/account/{biz}')
async def get_account(
    biz: str,
    storage: PostgresStorage = Depends(get_storage),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    """
    获取指定公众号的详细信息。

    Args:
        biz (str): 公众号唯一标识。

    Returns:
        dict: 公众号详情。

    Raises:
        ApiError: 如果公众号未找到。
    """
    return await _get_account(storage, user.id, biz)


@router.patch('/account/{biz}')
async def update_account(
    biz: str,
    body: dict[str, Any] = Body(default={}),
    storage: PostgresStorage = Depends(get_storage),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    """
    更新指定公众号的信息。

    Args:
        biz (str): 公众号唯一标识。
        body (dict): 需要更新的字段。

    Returns:
        dict: 更新后的公众号详情。
    """
    if 'group_id' in body and body['group_id'] is None:
        default_group = await ensure_default_group(storage, user.id, name=DEFAULT_GROUP_NAME)
        body['group_id'] = default_group.id
    return await _update_account(storage, user.id, biz, body)


@router.delete('/account/{biz}', status_code=status.HTTP_204_NO_CONTENT)
async def delete_account(
    biz: str,
    storage: PostgresStorage = Depends(get_storage),
    user: User = Depends(current_user),
) -> Response:
    """
    取消关注指定公众号。

    抓取目录是共享的，所以这里只解除当前用户的订阅：文章、图片和 S3 资源都保留，
    以便其他订阅者继续阅读、也避免重抓成本。当最后一个订阅者离开时，worker
    会自动停止抓取该号。

    Args:
        biz (str): 公众号唯一标识。

    Returns:
        Response: HTTP 204 No Content。

    Raises:
        ApiError: 如果该用户并未关注这个公众号。
    """
    async with storage.transaction():
        removed = await storage.subscriptions.remove_many(user.id, [biz])
    if removed == 0:
        raise ApiError('Account not found', status=404)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get('/account/{biz}/avatar')
async def get_account_avatar(
    biz: str,
    storage: PostgresStorage = Depends(get_storage),
) -> Response:
    """
    获取指定公众号的头像。

    Args:
        biz (str): 公众号唯一标识。

    Returns:
        Response: 包含正确 Content-Type 的图片数据。

    Raises:
        ApiError: 如果头像或公众号未找到。
    """
    avatar = await _get_avatar_row(storage, biz)
    data = avatar.get('data') if avatar else None
    if not data:
        url = avatar.get('avatar_url') if avatar else None
        if not url:
            row = await fetchone_row(
                storage,
                'SELECT round_head_img FROM accounts WHERE biz = %s',
                [biz],
                normalize=_normalize_record,
            )
            if not row:
                raise ApiError('Account not found', status=404)
            url = row.get('round_head_img')
            if url:
                await _upsert_avatar_url(storage, biz, url)
        if url:
            cached = await _fetch_and_cache_avatar(storage, biz, url)
            if cached:
                payload, content_type = cached
                return binary_response(payload, content_type)
        raise ApiError('Avatar not found', status=404)
    payload = data.tobytes() if isinstance(data, memoryview) else bytes(data)
    content_type = avatar.get('content_type') or 'application/octet-stream'
    return binary_response(payload, content_type)
