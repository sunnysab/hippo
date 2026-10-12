"""Subscribing by pasting a URL, and per-user source credentials.

One URL is the whole interface: the reader pastes a video, a channel or a feed, and
the preview says what it resolved to before anything is written. A source that
needs a login (Bilibili's listing is risk-controlled) uses the caller's own cookie
when they have one, and the instance's otherwise.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, Query, Request, status

from ...config import DEFAULT_GROUP_NAME
from ...exceptions import ApiError
from ...logger import get_logger
from ...media_settings import get_media_settings, instance_credential, resolve_proxy
from ...models import AccountCredential, User
from ...sources import UnsupportedSource
from ...sources.credentials import SOURCES as CREDENTIAL_SOURCES
from ...sources.credentials import CredentialError
from ...sources.credentials import clean as clean_credential
from ...sources.detect import detect_source
from ...sources.probe import probe_credential
from ...sources.registry import MediaSources
from ...storage import PostgresStorage, ensure_default_group
from ..deps import client_ip, current_user, get_storage

logger = get_logger(__name__)

router = APIRouter(dependencies=[Depends(current_user)])

#: Sources whose credential hippo actually reads. A user cookie is only used for
#: what the user triggers; the background sync runs with the instance credential.
#: Both names and their checking rules live in ``sources.credentials`` so the
#: console and this endpoint cannot drift apart.


async def _media_sources(storage: PostgresStorage, user: User) -> MediaSources:
    settings = await get_media_settings(storage)
    if not settings.get('enabled'):
        raise ApiError('媒体来源未启用，请先在管理台「媒体与转写」里打开', status=400)
    cookie = await storage.credentials.get(user.id, 'bilibili')
    cookies = await storage.credentials.get(user.id, 'youtube')
    try:
        return MediaSources(
            storage=storage,
            settings=settings,
            bilibili_cookie=cookie,
            youtube_cookies=cookies,
        )
    except Exception as exc:
        raise ApiError(f'媒体来源初始化失败：{exc}', status=500) from exc


def _require_source(url: str) -> str:
    source = detect_source(url)
    if source is None:
        raise ApiError('请输入 http(s) 订阅地址', status=400)
    return source


@router.get('/source')
async def list_sources(
    user: User = Depends(current_user),
    storage: PostgresStorage = Depends(get_storage),
) -> dict[str, Any]:
    """The sources this user actually follows, for the article rail.

    A source with no subscription is not offered as a filter, and no source is
    hidden: an unknown one shows up with its raw identifier (see ``SOURCE_META``).
    """
    return {'sources': await storage.accounts.count_by_source(user.id)}


@router.get('/source/preview')
async def preview_source(
    url: str = Query(..., description='视频 / 频道 / 订阅地址'),
    user: User = Depends(current_user),
    storage: PostgresStorage = Depends(get_storage),
) -> dict[str, Any]:
    """Resolve a pasted URL, changing nothing.

    This is the step that turns "@某个UP" into a channel id and a name, so the
    console can show what is about to be subscribed to.
    """
    source = _require_source(url)
    sources = await _media_sources(storage, user)
    try:
        try:
            info = await sources.preview(source, url)
        except UnsupportedSource as exc:
            raise ApiError(str(exc), status=400) from exc
        except ValueError as exc:
            raise ApiError(str(exc), status=400) from exc
        except Exception as exc:
            raise ApiError(f'解析失败：{exc}', status=502) from exc
    finally:
        await sources.aclose()
    try:
        await storage.accounts.get_account(info.biz)
        subscribed = True
    except LookupError:
        subscribed = False
    return {
        # preview 认出来的优先：feed 里有没有音频附件，只有抓下来才知道。
        'source': info.source or source,
        'biz': info.biz,
        'nickname': info.nickname,
        'avatar_url': info.avatar_url,
        'subscribed': subscribed,
    }


@router.post('/source/channel', status_code=status.HTTP_202_ACCEPTED)
async def subscribe_source(
    request: Request,
    body: dict[str, Any] = Body(default={}),
    user: User = Depends(current_user),
    storage: PostgresStorage = Depends(get_storage),
) -> dict[str, Any]:
    """Subscribe the caller to whatever ``url`` points at.

    Accepts the ``biz`` from a preview so the console does not resolve the same URL
    twice; without it the URL is resolved here.
    """
    url = str(body.get('url') or '').strip()
    if not url:
        raise ApiError('缺少 url', status=400)
    source = _require_source(url)

    group_id = body.get('group_id')
    if group_id is None:
        default_group = await ensure_default_group(storage, user.id, name=DEFAULT_GROUP_NAME)
        group_id = default_group.id

    sources = await _media_sources(storage, user)
    try:
        try:
            info = await sources.preview(source, url)
        except (UnsupportedSource, ValueError) as exc:
            raise ApiError(str(exc), status=400) from exc
        except Exception as exc:
            raise ApiError(f'解析失败：{exc}', status=502) from exc
    finally:
        await sources.aclose()

    async with storage.transaction():
        account = await storage.accounts.upsert_account(
            AccountCredential(
                biz=info.biz,
                nickname=info.nickname,
                # preview 认出来的优先：feed 里有没有音频附件，只有抓下来才知道，
                # 所以 podcast / feed 的区分在这里才落地。
                source=info.source or source,
                # alias 存来源内的标识（mid / channel id / feed URL）
                alias=info.source_key,
                round_head_img=info.avatar_url,
            )
        )
        await storage.subscriptions.upsert(user.id, account.biz, group_id=int(group_id))
        # 订阅当下就跑一轮列表：媒体来源的正文要排队转写，越早入队越好。
        await storage.sync_jobs.create_job(trigger_type='manual', biz_list=[account.biz])
        await storage.audit.record(
            user.id,
            'source.subscribed',
            target=account.biz,
            detail={'source': source},
            ip=client_ip(request),
        )
    logger.info('订阅 %s（%s）', account.biz, source)
    return {
        'biz': account.biz,
        'nickname': account.nickname,
        'source': source,
        'group_id': group_id,
    }


@router.get('/source/credential')
async def list_credentials(
    user: User = Depends(current_user),
    storage: PostgresStorage = Depends(get_storage),
) -> dict[str, Any]:
    """Which sources this user brought their own login for. Never the value."""
    configured = await storage.credentials.configured(user.id)
    return {source: {'configured': bool(configured.get(source))} for source in CREDENTIAL_SOURCES}


@router.put('/source/credential/{source}', status_code=status.HTTP_204_NO_CONTENT)
async def set_credential(
    source: str,
    body: dict[str, Any] = Body(default={}),
    user: User = Depends(current_user),
    storage: PostgresStorage = Depends(get_storage),
) -> None:
    """Store the caller's own cookie. An empty secret clears it.

    The value is never read back out of the API — not here, and not from the list
    endpoint, which only reports whether one is configured.
    """
    try:
        secret = clean_credential(source, body.get('secret'))
    except CredentialError as exc:
        raise ApiError(str(exc), status=400) from exc
    async with storage.transaction():
        await storage.credentials.set(user.id, source, secret)


@router.post('/source/credential/{source}/test')
async def test_credential(
    source: str,
    scope: str = Query('user', pattern='^(user|instance)$'),
    user: User = Depends(current_user),
    storage: PostgresStorage = Depends(get_storage),
) -> dict[str, Any]:
    """Ask the source whether a credential actually works.

    ``user`` tests the caller's own cookie, falling back to the instance one and
    saying which answered; ``instance`` asks about the shared one and is admin-only.
    Neither ever returns the credential, only the account it was accepted as.
    """
    if source not in CREDENTIAL_SOURCES:
        raise ApiError(f'不支持测试 {source} 的凭证', status=400)
    if scope == 'instance' and getattr(user, 'role', '') != 'admin':
        raise ApiError('只有管理员能测试实例凭证', status=403)
    settings = await get_media_settings(storage)
    used = scope
    cookie = ''
    if scope == 'user':
        cookie = await storage.credentials.get(user.id, source) or ''
        if not cookie:
            used = 'instance'
    if not cookie:
        cookie = instance_credential(dict(settings), source)
    if not cookie:
        return {
            'source': source,
            'credential': 'none',
            'ok': False,
            'detail': '没有可测试的凭证：先提交一份，或在管理台配置实例凭证',
            'account': '',
        }
    probe = await probe_credential(source=source, cookie=cookie, proxy=resolve_proxy(dict(settings), source))
    return {
        'source': source,
        'credential': used,
        'ok': probe.ok,
        'detail': probe.detail,
        'account': probe.account,
    }


@router.delete('/source/credential/{source}', status_code=status.HTTP_204_NO_CONTENT)
async def delete_credential(
    source: str,
    user: User = Depends(current_user),
    storage: PostgresStorage = Depends(get_storage),
) -> None:
    if source not in CREDENTIAL_SOURCES:
        raise ApiError(f'{source} 没有可清除的凭证', status=400)
    async with storage.transaction():
        await storage.credentials.delete(user.id, source)


__all__ = ['CREDENTIAL_SOURCES', 'router']
