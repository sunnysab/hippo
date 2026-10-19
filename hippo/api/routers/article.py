"""Article and image endpoints."""

import asyncio
import json
import threading
import time as time_module
import uuid
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse
from starlette.concurrency import iterate_in_threadpool

from ...article_queries import (
    _assert_article_visible,
    _block_image,
    _fetch_image,
    _get_article,
    _list_article_images,
    _list_articles,
    _normalize_article_sort,
    _normalize_item_show_type,
    _normalize_read_filter,
    _normalize_source_filter,
    _normalize_starred_filter,
    _parse_date,
    _split_article_exclude_keywords,
)
from ...container import build_downloader_container
from ...exceptions import ApiError
from ...logger import get_logger
from ...media import archive as archive_module
from ...media import audio_export
from ...media.subtitle import to_markdown, to_srt, to_text
from ...media_settings import get_media_settings, resolve_proxy
from ...models import User
from ...storage import PostgresStorage, fetchone_row, open_storage
from ..deps import current_user, get_storage, parse_group_ids
from ..responses import binary_response
from ..schemas.article import (
    ArticleAudioOut,
    ArticleDetailOut,
    ArticleImageListOut,
    ArticleListOut,
    BlockedImageOut,
    ReadStateOut,
    ReadUpdateRequest,
    RefetchStartedOut,
    RefetchStatusOut,
    StarStateOut,
    StarUpdateRequest,
    TranscribeQueuedOut,
)

logger = get_logger(__name__)
router = APIRouter(dependencies=[Depends(current_user)])

_refetch_tasks: dict[str, dict[str, Any]] = {}
_refetch_lock = threading.Lock()


@router.get('/article', response_model=ArticleListOut, response_model_exclude_unset=True)
async def list_articles(
    group_id: int | None = None,
    group_ids: str | None = None,
    biz: str | None = None,
    item_show_type: int | None = None,
    article_id: str | None = None,
    q: str | None = None,
    exclude_keywords: str | None = None,
    sort: str | None = None,
    read: str | None = None,
    starred: str | None = None,
    source: str | None = None,
    page: int = 1,
    page_size: int = 20,
    since: str | None = None,
    until: str | None = None,
    storage: PostgresStorage = Depends(get_storage),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    """
    列出文章，支持多种筛选条件。

    Args:
        group_id (int | None): 按单个分组 ID 筛选 (向后兼容)。
        group_ids (str | None): 按多个分组 ID 筛选，逗号分隔 (如 "1,2,3")。
        biz (str | None): 按公众号 biz 筛选。
        article_id (str | None): 按具体文章 ID 筛选 (微信原始 article_id)。
        q (str | None): 搜索文章内容/标题。
        read (str | None): 传 `unread` 只返回未读文章。
        starred (str | None): 传 `true` 只返回已收藏文章。
        source (str | None): 按来源筛选，逗号分隔（如 "weixin"）。
        page (int): 页码。
        page_size (int): 每页数量。
        since (str | None): 起始日期筛选 (ISO 格式)。
        until (str | None): 结束日期筛选 (ISO 格式)。

    Returns:
        dict: 文章列表和分页信息。
    """
    since_ts = _parse_date(since)
    until_ts = _parse_date(until, end_of_day=True)
    query_text = (q or '').strip()
    exclude_source = exclude_keywords
    if exclude_source is None:
        # Filters are personal: two readers of the same feed legitimately want
        # different exclusion lists.
        preferences = await storage.users.get_preferences(user.id)
        exclude_source = str(preferences.get('article_exclude_keywords') or '')
    exclude_terms = _split_article_exclude_keywords(exclude_source)
    sort_mode = _normalize_article_sort(sort, has_query=bool(query_text))
    normalized_item_show_type = _normalize_item_show_type(item_show_type)
    return await _list_articles(
        storage,
        user_id=user.id,
        group_ids=parse_group_ids(group_ids, group_id),
        biz=biz or None,
        item_show_type=normalized_item_show_type,
        query=query_text or None,
        exclude_keywords=exclude_terms or None,
        since_ts=since_ts,
        until_ts=until_ts,
        sort_mode=sort_mode,
        page=max(page, 1),
        page_size=min(max(page_size, 1), 200),
        article_id=article_id or None,
        unread_only=_normalize_read_filter(read),
        starred_only=_normalize_starred_filter(starred),
        sources=_normalize_source_filter(source),
    )


@router.get(
    '/article/{article_id}',
    response_model=ArticleDetailOut,
    # Blocks and the relevance score are written by the source that produced
    # them: only the keys that are really there come back.
    response_model_exclude_unset=True,
)
async def get_article(
    article_id: int,
    storage: PostgresStorage = Depends(get_storage),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    """
    获取指定文章的完整详情。

    Args:
        article_id (int): 文章的主键 ID。

    Returns:
        dict: 文章详情，包括内容和图片。

    Raises:
        ApiError: 如果文章未找到。
    """
    return await _get_article(storage, user.id, article_id)


@router.put('/article/{article_id}/read', response_model=ReadStateOut)
async def set_article_read(
    article_id: int,
    body: ReadUpdateRequest,
    storage: PostgresStorage = Depends(get_storage),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    """
    标记单篇文章的已读状态。

    Args:
        article_id (int): 文章 ID。
        body (ReadUpdateRequest): `{"read": true}` 标记已读，`false` 标记未读（默认 true）。

    Returns:
        dict: 文章 ID 与已读时间（未读时为 null）。
    """
    await _assert_article_visible(storage, user.id, article_id)
    read_at = await storage.reads.set_read(user_id=user.id, article_pk=article_id, read=body.read)
    return {'article_id': article_id, 'read_at': read_at}


@router.put('/article/{article_id}/star', response_model=StarStateOut)
async def set_article_star(
    article_id: int,
    body: StarUpdateRequest,
    storage: PostgresStorage = Depends(get_storage),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    """
    收藏 / 取消收藏单篇文章。

    Args:
        article_id (int): 文章 ID。
        body (StarUpdateRequest): `{"starred": true}` 收藏，`false` 取消（默认 true）。

    Returns:
        dict: 文章 ID 与收藏时间（未收藏时为 null）。
    """
    await _assert_article_visible(storage, user.id, article_id)
    starred_at = await storage.stars.set_starred(user_id=user.id, article_pk=article_id, starred=body.starred)
    return {'article_id': article_id, 'starred_at': starred_at}


@router.get('/article/{article_id}/image', response_model=ArticleImageListOut)
async def list_article_images(
    article_id: int,
    storage: PostgresStorage = Depends(get_storage),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    """
    列出与文章关联的图片。

    Args:
        article_id (int): 文章 ID。

    Returns:
        dict: 图片元数据列表。
    """
    payload = await _list_article_images(storage, user.id, article_id)
    return {'images': payload}


@router.post('/article/{article_id}/transcribe', status_code=202, response_model=TranscribeQueuedOut)
async def transcribe_article(
    article_id: int,
    force_asr: bool = Query(False, description='跳过现成的字幕/转录稿，强制重新转写'),
    storage: PostgresStorage = Depends(get_storage),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    """Queue a transcribed article again — the reader's "转写这篇".

    ``force_asr`` is for the case a published subtitle is worse than the model:
    the flag rides along in the job payload and the resolver skips it.
    """
    await _assert_article_visible(storage, user.id, article_id)
    job = await storage.media_jobs.get(article_id)
    if not job:
        raise ApiError('这篇文章没有转写任务（不是媒体条目）', status=404)
    payload = dict(job.get('payload') or {})
    if force_asr:
        payload['skip_subtitle'] = True
    async with storage.transaction():
        await storage.media_jobs.enqueue_many(
            [
                {
                    'article_pk': article_id,
                    'source': job['source'],
                    'media_url': job['media_url'],
                    'payload': payload,
                }
            ],
            force=True,
        )
    logger.info('按需转写 article_pk=%s force_asr=%s', article_id, force_asr)
    return {'article_pk': article_id, 'state': 'pending', 'force_asr': force_asr}


def _audio_answer(state: str, article_id: int) -> dict[str, Any]:
    stored = state == 'stored'
    return {'state': state, 'url': f'/api/media/{article_id}/audio' if stored else None}


@router.get('/article/{article_id}/audio', response_model=ArticleAudioOut)
async def get_article_audio(
    article_id: int,
    storage: PostgresStorage = Depends(get_storage),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    """Whether this article can be listened to without its picture, and start nothing."""
    await _assert_article_visible(storage, user.id, article_id)
    return _audio_answer(await audio_export.audio_state(storage, article_id), article_id)


@router.post('/article/{article_id}/audio', status_code=202, response_model=ArticleAudioOut)
async def start_article_audio(
    article_id: int,
    storage: PostgresStorage = Depends(get_storage),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    """Lift the audio out of the archived video, for articles archived before that existed.

    New transcriptions keep both files already; this is the repair path, and it is
    idempotent — a second request joins the extraction that is already running.
    """
    await _assert_article_visible(storage, user.id, article_id)
    return _audio_answer(await audio_export.ensure_audio(storage, article_id), article_id)


#: The transcript exports are text formats; the body is the document itself, not JSON.
_TRANSCRIPT_RESPONSES = {
    200: {
        'description': 'The transcript, in the requested format.',
        'content': {
            'text/markdown': {},
            'text/plain': {},
            'application/x-subrip': {},
        },
    }
}


@router.get(
    '/article/{article_id}/transcript', response_class=Response, response_model=None, responses=_TRANSCRIPT_RESPONSES
)
async def export_transcript(
    article_id: int,
    format: str = Query('md', pattern='^(md|txt|srt)$'),
    storage: PostgresStorage = Depends(get_storage),
    user: User = Depends(current_user),
) -> Response:
    """Export the transcript as markdown, plain text or subtitles.

    This is the endpoint a script or a feed reader uses to take the words away: the
    timestamps live in the content blocks, so the export needs nothing else.
    """
    await _assert_article_visible(storage, user.id, article_id)
    row = await fetchone_row(
        storage,
        'SELECT content_json FROM article_content WHERE article_pk = %s',
        [article_id],
    )
    blocks: Any = row.get('content_json') if row else None
    if isinstance(blocks, str):
        try:
            blocks = json.loads(blocks)
        except json.JSONDecodeError:
            blocks = None
    if not blocks:
        raise ApiError('这篇还没有正文', status=404)

    if format == 'srt':
        body = to_srt(blocks)
        media_type = 'application/x-subrip; charset=utf-8'
    elif format == 'txt':
        body = to_text(blocks)
        media_type = 'text/plain; charset=utf-8'
    else:
        body = to_markdown(blocks)
        media_type = 'text/markdown; charset=utf-8'
    if not body.strip():
        # 有正文但没有段落（例如只有一块视频）时，导出个空文件没有意义。
        raise ApiError('这篇没有可导出的转写稿', status=404)
    return Response(content=body, media_type=media_type)


#: The archived object is handed out with the content type it was stored under.
_ARCHIVED_MEDIA_RESPONSES = {
    200: {
        'description': 'The archived bytes, in the type the object was stored with.',
        'content': {'audio/mpeg': {}, 'video/mp4': {}, 'application/octet-stream': {}},
    },
    206: {
        'description': 'The requested byte range of the archived object.',
        'content': {'audio/mpeg': {}, 'video/mp4': {}, 'application/octet-stream': {}},
    },
    302: {
        'description': 'Audio that was never archived, redirected to its source URL.',
    },
    416: {
        'description': 'The requested range is not satisfiable.',
        'headers': {'Content-Range': {'schema': {'type': 'string'}}},
    },
}


@router.get(
    '/media/{article_pk}/{kind}', response_class=Response, response_model=None, responses=_ARCHIVED_MEDIA_RESPONSES
)
async def stream_archived_media(
    article_pk: int,
    kind: str,
    request: Request,
    storage: PostgresStorage = Depends(get_storage),
    user: User = Depends(current_user),
) -> Response:
    """Stream an article's archived media, with range support so the reader can seek.

    Same handler for audio and video: both are an object in the store and a content
    type. Visibility comes first, so a foreign article never reaches the store.
    Only audio keeps the original-URL fallback: for a video that URL is a
    third-party page, not playable bytes, and a redirect would hand the reader
    HTML instead of a 404 they can turn into "no player".
    """
    if kind not in archive_module.MEDIA_KINDS:
        raise ApiError(f'不认识的媒体类型：{kind}', status=404)
    await _assert_article_visible(storage, user.id, article_pk)
    asset = await storage.media_assets.get(article_pk, kind=kind)
    s3_key = str(asset.get('s3_key') or '').strip() if asset else ''
    if not asset or asset.get('state') != 'stored' or not s3_key:
        if kind == 'audio':
            fallback = _original_media_url(asset) if asset else None
            if fallback:
                return RedirectResponse(fallback, status_code=302)
        raise ApiError(f'这篇文章没有留档{kind}', status=404)

    total = _known_size(asset.get('size_bytes'))
    try:
        start, end = archive_module.parse_range(request.headers.get('range'), total)
    except archive_module.ArchiveError as exc:
        return _range_not_satisfiable(str(exc), total)
    try:
        stream = await asyncio.to_thread(archive_module.open_audio, s3_key, start=start, end=end)
    except archive_module.ArchiveError as exc:
        raise ApiError(str(exc), status=502, code='article.archive_unavailable') from exc

    headers = {
        'Accept-Ranges': 'bytes',
        'Content-Length': str(stream.size),
        'Cache-Control': 'private, max-age=3600',
    }
    status_code = 200
    if start is not None:
        headers['Content-Range'] = f'bytes {start}-{start + stream.size - 1}/{stream.total}'
        status_code = 206
    return StreamingResponse(
        _close_body_when_done(stream.body),
        status_code=status_code,
        media_type=stream.content_type,
        headers=headers,
    )


def _original_media_url(asset: dict[str, Any] | None) -> str | None:
    """The source URL for an unarchived audio asset, when it is safe to redirect to.

    ``orig_url`` is provider metadata we did not author, so a scheme the browser
    cannot play (``javascript:``, ``data:``, ``file:``) or a URL without a host is
    refused rather than handed out as a redirect. Only the audio path uses this: a
    video's original URL points at a third-party page, not at bytes.
    """
    if not asset:
        return None
    raw = asset.get('orig_url')
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip()
    try:
        parsed = urlparse(text)
        hostname = parsed.hostname
    except ValueError:
        return None
    if parsed.scheme not in ('http', 'https') or not hostname:
        return None
    return text


def _known_size(value: Any) -> int | None:
    """``article_media.size_bytes`` is nullable, and a range needs a real number."""
    try:
        total = int(value)
    except TypeError, ValueError:
        return None
    return total if total >= 0 else None


def _range_not_satisfiable(message: str, total: int | None) -> JSONResponse:
    """416 with the real length, so the client can restart from the last byte.

    Same ``{'error', 'code'}`` shape the API error handler produces; only this
    response carries ``Content-Range``, which ``ApiError`` cannot. Without a known
    total there is no length to report and the header is omitted rather than
    invented.
    """
    headers = {'Content-Range': f'bytes */{total}'} if total is not None else None
    return JSONResponse(
        status_code=416,
        content={'error': message, 'code': 'http_416'},
        headers=headers,
    )


async def _close_body_when_done(body: Any) -> AsyncIterator[bytes]:
    """Yield the S3 body and close it whatever happened.

    Starlette stops iterating on a client disconnect and on a failed chunk, so
    ``finally`` is the only place the upstream connection is reliably released.
    """
    try:
        async for chunk in iterate_in_threadpool(body):
            yield chunk
    finally:
        close = getattr(body, 'close', None)
        if close is not None:
            try:
                # Closing can block on the upstream socket, so it is not done on the loop.
                await asyncio.to_thread(close)
            except Exception:
                logger.warning('Failed to close archived media body', exc_info=True)


@router.get(
    '/image/{image_id}', response_class=Response, response_model=None, responses={200: {'content': {'image/*': {}}}}
)
async def get_image(
    image_id: int,
    storage: PostgresStorage = Depends(get_storage),
    user: User = Depends(current_user),
) -> Response:
    """
    通过 ID 获取图片内容。
    如果存储在 S3 中，则从 S3 获取。否则从源地址获取。

    Args:
        image_id (int): 图片 ID。

    Returns:
        Response: 图片二进制数据。
    """
    payload, content_type = await _fetch_image(storage, user.id, image_id)
    return binary_response(payload, content_type)


@router.post('/image/{image_id}/block', response_model=BlockedImageOut)
async def block_image(
    image_id: int,
    storage: PostgresStorage = Depends(get_storage),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    """
    Block an image globally by its binary content hash.

    Args:
        image_id (int): Image ID.

    Returns:
        dict: Blocking result and resolved hash.
    """
    return await _block_image(storage, user.id, image_id)


def _cleanup_refetch_tasks() -> None:
    now = time_module.monotonic()
    with _refetch_lock:
        stale = [
            tid
            for tid, t in _refetch_tasks.items()
            if t.get('status') in ('done', 'error') and now - t.get('finished_at', 0) > 300
        ]
        for tid in stale:
            del _refetch_tasks[tid]


@router.get('/article/refetch/{task_id}', response_model=RefetchStatusOut)
def get_refetch_status(task_id: str, user: User = Depends(current_user)) -> dict[str, Any]:
    _cleanup_refetch_tasks()
    with _refetch_lock:
        # Snapshot while holding the lock: the worker mutates the live entry
        # (it may add finished_at) under the same lock, so reading it afterwards
        # could see a partial update or a resized dict.
        task = dict(_refetch_tasks.get(task_id) or {})
    # A task id is not a capability: only the reader who started it may poll it,
    # and a foreign id is indistinguishable from one that never existed.
    if not task or task.get('user_id') != user.id:
        raise ApiError('Task not found', status=404)
    return {key: value for key, value in task.items() if key != 'user_id'}


@router.post('/article/{article_id}/refetch', response_model=RefetchStartedOut)
async def refetch_article(
    article_id: int,
    storage: PostgresStorage = Depends(get_storage),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    await _assert_article_visible(storage, user.id, article_id)
    row = await fetchone_row(
        storage,
        # An account row that is gone leaves the source unknown, and the empty
        # string keeps the refetch on the instance default proxy.
        "SELECT a.biz, a.article_id, a.link, COALESCE(acc.source, '') AS account_source"
        ' FROM articles a LEFT JOIN accounts acc ON acc.biz = a.biz'
        ' WHERE a.id = %s',
        [article_id],
    )
    if not row:
        raise ApiError('Article not found', status=404)
    link = row.get('link')
    if not link:
        raise ApiError('Article has no source URL', status=400)
    # 重新抓取要落回原行：传原文的 biz + article_id，让 upsert 命中同一行
    # （默认的 adhoc 只适用于手贴的、没有归属的链接）。
    owner_biz = str(row.get('biz') or '')
    owner_article_id = str(row.get('article_id') or '')
    account_source = str(row.get('account_source') or '')

    task_id = uuid.uuid4().hex
    started_at = time_module.monotonic()
    with _refetch_lock:
        _refetch_tasks[task_id] = {
            'task_id': task_id,
            # Internal owner bookkeeping; never returned by the status endpoint.
            'user_id': user.id,
            'status': 'running',
            'phase': 'downloading',
            'started_at': started_at,
            'error': None,
        }
    _cleanup_refetch_tasks()

    def _run() -> None:
        async def _do() -> None:
            async with open_storage() as thread_storage:
                media_settings = await get_media_settings(thread_storage)
                proxy = (
                    resolve_proxy(media_settings, account_source)
                    if account_source
                    else str(media_settings.get('proxy_default') or '')
                )
                container = build_downloader_container(
                    storage=thread_storage,
                    enable_images=True,
                    proxy=proxy,
                )
                async with container as app:
                    downloader = app.downloader
                    if not downloader:
                        raise RuntimeError('Downloader not initialized')
                    await downloader.download_from_url(
                        str(link),
                        with_images=True,
                        biz=owner_biz,
                        article_id=owner_article_id,
                    )
            with _refetch_lock:
                _refetch_tasks[task_id]['status'] = 'done'
                _refetch_tasks[task_id]['phase'] = 'done'
                _refetch_tasks[task_id]['finished_at'] = time_module.monotonic()

        try:
            asyncio.run(_do())
        except Exception as exc:
            logger.warning('Article refetch failed (task=%s): %s', task_id, exc)
            with _refetch_lock:
                _refetch_tasks[task_id]['status'] = 'error'
                _refetch_tasks[task_id]['phase'] = 'error'
                _refetch_tasks[task_id]['error'] = str(exc)
                _refetch_tasks[task_id]['finished_at'] = time_module.monotonic()

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()

    return {'task_id': task_id, 'status': 'started'}
