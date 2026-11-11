"""Article and image endpoints."""

import asyncio
import json
import re
import shutil
import tempfile
import threading
import time as time_module
import uuid
import zipfile
from collections.abc import AsyncIterator
from pathlib import Path as FilePath
from typing import Any
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, Path, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from starlette.background import BackgroundTask
from starlette.concurrency import iterate_in_threadpool

from ...article_queries import (
    _assert_article_visible,
    _block_image,
    _fetch_image,
    _filter_blocked_content_blocks,
    _get_article,
    _get_visible_article_images,
    _list_article_images,
    _list_articles,
    _list_watch_later,
    _move_watch_later,
    _normalize_article_sort,
    _normalize_item_show_type,
    _normalize_read_filter,
    _normalize_source_filter,
    _normalize_starred_filter,
    _normalize_topic_filter,
    _parse_date,
    _project_transcript,
    _split_article_exclude_keywords,
)
from ...article_references import article_references
from ...container import build_downloader_container
from ...exceptions import ApiError
from ...image_hashes import fetch_image_bytes
from ...logger import get_logger
from ...media import archive as archive_module
from ...media import audio_export
from ...media.subtitle import to_markdown, to_srt, to_text
from ...media_settings import get_media_settings, instance_credential, resolve_proxy
from ...models import ArticleRecord, User
from ...s3 import guess_extension
from ...site_settings import get_site_settings
from ...sources.caixin import CaixinSource
from ...sources.caixin_pacing import CaixinPacer
from ...sources.caixin_sync import caixin_article_access, caixin_channel_is_collectable, ingest_caixin_body
from ...storage import PostgresStorage, fetchall_rows, fetchone_row, open_storage
from ..deps import current_user, get_storage, parse_group_ids
from ..responses import binary_response
from ..schemas.article import (
    ArticleAudioOut,
    ArticleDetailOut,
    ArticleImageListOut,
    ArticleListOut,
    ArticleReferenceFetchRequest,
    ArticleReferenceListOut,
    BlockedImageOut,
    ReadStateOut,
    ReadUpdateRequest,
    RefetchStartedOut,
    RefetchStatusOut,
    StarStateOut,
    StarUpdateRequest,
    TranscribeQueuedOut,
    WatchLaterListOut,
    WatchLaterMoveOut,
    WatchLaterMoveRequest,
    WatchLaterStateOut,
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
    topic: str | None = None,
    event_id: int | None = None,
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
        topic (str | None): 按方向筛选，逗号分隔（如 "ai,research"）；未知方向返回 400。
        event_id (int | None): 只看同一件事的报道；事件未达可见门槛时返回 404。
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
    vocabulary = (await get_site_settings(storage))['vocabulary']
    if event_id is not None and await storage.topics.public_event(event_id, user.id) is None:
        # Same rule as the feed: one publisher's guess is not a story.
        raise ApiError('Event not found', status=404)
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
        topics=_normalize_topic_filter(topic, vocabulary),
        vocabulary=vocabulary,
        event_id=event_id,
    )


def _export_path_component(value: Any) -> str:
    return re.sub(r'[\x00-\x1f\\/:*?"<>|]+', '_', str(value)).strip(' .')[:80] or 'untitled'


async def _export_thread_call(function: Any, *args: Any, **kwargs: Any) -> Any:
    task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await asyncio.gather(task, return_exceptions=True)
        raise


@router.get('/article/export')
async def export_articles(
    biz: list[str] = Query(min_length=1, max_length=100),
    format: str = 'markdown',
    storage: PostgresStorage = Depends(get_storage),
    user: User = Depends(current_user),
) -> Response:
    """Export every collected article for one or more of the caller's accounts."""
    accounts = list(dict.fromkeys(value.strip() for value in biz if value.strip()))
    if not accounts or format not in {'markdown', 'json'}:
        raise ApiError('Invalid export request', status=400)
    subscribed = await fetchall_rows(
        storage,
        'SELECT biz FROM subscription WHERE user_id = %s AND biz = ANY(%s)',
        [user.id, accounts],
    )
    if {row['biz'] for row in subscribed} != set(accounts):
        raise ApiError('Account not found', status=404)
    work_dir = FilePath(tempfile.mkdtemp(prefix='hippo-export-'))
    zip_path = work_dir / 'hippo-articles.zip'
    archive: zipfile.ZipFile | None = None
    last_id = 0
    try:
        archive = zipfile.ZipFile(zip_path, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=9)
        while True:
            rows = await fetchall_rows(
                storage,
                """
                SELECT a.id, a.biz, acc.nickname AS account_nickname, a.title, a.author,
                       a.digest, a.link, a.source_url, a.publish_at,
                       COALESCE(c.content_markdown, '') AS content_markdown,
                       c.content_json
                  FROM articles a
                  JOIN subscription s ON s.biz = a.biz AND s.user_id = %s
                  JOIN accounts acc ON acc.biz = a.biz
                  LEFT JOIN article_content c ON c.article_pk = a.id
                 WHERE a.biz = ANY(%s) AND a.id > %s
                 ORDER BY a.id
                 LIMIT 100
                """,
                [user.id, accounts, last_id],
            )
            if not rows:
                break
            last_id = int(rows[-1]['id'])
            for row in rows:
                content = row.get('content_json')
                if isinstance(content, str):
                    try:
                        content = json.loads(content)
                    except json.JSONDecodeError:
                        content = None
                account_dir = (
                    f'accounts/{_export_path_component(row["account_nickname"])}-{_export_path_component(row["biz"])}'
                )
                article_dir = f'{account_dir}/{row["id"]}-{_export_path_component(row["title"])}'
                attachments: list[str] = []
                replacements: list[tuple[str, str]] = []
                blocked_urls: list[str] = []
                image_rows = await fetchall_rows(
                    storage,
                    'SELECT id, orig_url FROM article_images WHERE article_pk = %s ORDER BY position',
                    [row['id']],
                )
                visible_images, blocked_image_ids = await _get_visible_article_images(storage, int(row['id']))
                visible_image_ids = {int(image['id']) for image in visible_images}
                if isinstance(content, list):
                    content = _filter_blocked_content_blocks(content, blocked_image_ids)
                for image in image_rows:
                    image_id = int(image['id'])
                    if image_id not in visible_image_ids:
                        if image.get('orig_url'):
                            blocked_urls.append(str(image['orig_url']))
                        continue
                    try:
                        image_bytes, content_type = await fetch_image_bytes(storage, image_id, allow_origin_fetch=False)
                    except LookupError, RuntimeError:
                        continue
                    relative_path = f'attachments/images/{image_id}.{guess_extension(content_type)}'
                    await _export_thread_call(
                        archive.writestr,
                        f'{article_dir}/{relative_path}',
                        image_bytes,
                        compress_type=zipfile.ZIP_DEFLATED,
                        compresslevel=9,
                    )
                    attachments.append(relative_path)
                    if image.get('orig_url'):
                        replacements.append((str(image['orig_url']), relative_path))
                media_rows = await fetchall_rows(
                    storage,
                    "SELECT kind, content_type, s3_key FROM article_media WHERE article_pk = %s AND state = 'stored' AND s3_key IS NOT NULL",
                    [row['id']],
                )
                for media in media_rows:
                    kind = re.sub(r'[^a-z0-9_-]', '_', str(media['kind']).lower())
                    content_type = str(media.get('content_type') or '').split(';', 1)[0].strip().lower()
                    extension = 'mp4' if content_type == 'video/mp4' else guess_extension(content_type)
                    relative_path = f'attachments/media/{kind}.{extension}'
                    staged_path = work_dir / f'media-{row["id"]}-{kind}'
                    try:
                        await _export_thread_call(archive_module.fetch_object, str(media['s3_key']), staged_path)
                    except archive_module.ArchiveError:
                        continue
                    await _export_thread_call(
                        archive.write,
                        staged_path,
                        arcname=f'{article_dir}/{relative_path}',
                        compress_type=zipfile.ZIP_DEFLATED,
                        compresslevel=9,
                    )
                    staged_path.unlink(missing_ok=True)
                    attachments.append(relative_path)
                markdown = str(row['content_markdown'] or '')
                for source_url, relative_path in replacements:
                    markdown = markdown.replace(f']({source_url})', f']({relative_path})')
                for source_url in blocked_urls:
                    markdown = re.sub(r'!\[[^\]]*\]\(' + re.escape(source_url) + r'\)', '', markdown)
                article = {
                    'id': row['id'],
                    'account': row['account_nickname'],
                    'biz': row['biz'],
                    'title': row['title'],
                    'author': row['author'],
                    'digest': row['digest'],
                    'link': row['link'],
                    'source_url': row['source_url'],
                    'publish_at': row['publish_at'],
                    'content_markdown': markdown,
                    'content': content,
                    'attachments': attachments,
                }
                if format == 'json':
                    payload = json.dumps(article, ensure_ascii=False, indent=2)
                else:
                    media_links = [
                        f'- [{FilePath(path).name}]({path})'
                        for path in attachments
                        if path.startswith('attachments/media/')
                    ]
                    if media_links:
                        markdown = f'{markdown}\n\n## 媒体附件\n\n' + '\n'.join(media_links)
                    payload = (
                        f'# {article["title"] or "Untitled"}\n\n公众号：{article["account"]}\n\n'
                        f'发布时间：{article["publish_at"] or "未知"}\n\n'
                        f'原文：{article["link"] or article["source_url"] or ""}\n\n{markdown}'
                    )
                article_filename = 'article.json' if format == 'json' else 'article.md'
                await _export_thread_call(
                    archive.writestr,
                    f'{article_dir}/{article_filename}',
                    payload,
                    compress_type=zipfile.ZIP_DEFLATED,
                    compresslevel=9,
                )
        await _export_thread_call(archive.close)
        return FileResponse(
            zip_path,
            filename='hippo-articles.zip',
            media_type='application/zip',
            headers={'Cache-Control': 'private, no-store'},
            background=BackgroundTask(shutil.rmtree, work_dir, ignore_errors=True),
        )
    except BaseException:
        try:
            if archive:
                await _export_thread_call(archive.close)
        except BaseException:
            logger.warning('Failed to close incomplete article export', exc_info=True)
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)
        raise


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
    # The request handle rolls back on close, so the write needs its own commit.
    async with storage.transaction():
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
    async with storage.transaction():
        starred_at = await storage.stars.set_starred(user_id=user.id, article_pk=article_id, starred=body.starred)
    return {'article_id': article_id, 'starred_at': starred_at}


@router.get('/watch-later', response_model=WatchLaterListOut, response_model_exclude_unset=True)
async def list_watch_later(
    storage: PostgresStorage = Depends(get_storage),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    """
    列出我的「稍后再看」，按我自己排好的顺序。

    只返回我现在还能读的文章：退订、停用或直接引用失效后，条目仍留在列表里，
    重新订阅就回到原位，但不会再出现在这个响应中。这里不套用信息流的分组、
    来源筛选和排除词——这是一份手工建的清单。

    Returns:
        dict: 文章列表（结构与文章列表的单条一致）与总数。
    """
    return await _list_watch_later(storage, user.id)


@router.put('/watch-later/{article_id}', response_model=WatchLaterStateOut)
async def add_to_watch_later(
    article_id: int = Path(gt=0, description='文章 ID。'),
    storage: PostgresStorage = Depends(get_storage),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    """
    把一篇文章放进稍后再看。

    幂等：重复调用保留第一次加入的时间与位置，不会把它顶到最前面。
    不可见的文章返回 `404`。

    Returns:
        dict: 文章 ID 与首次加入时间。
    """
    await _assert_article_visible(storage, user.id, article_id)
    async with storage.transaction():
        watch_later_at = await storage.watch_later.add(user_id=user.id, article_pk=article_id)
    return {'article_id': article_id, 'watch_later_at': watch_later_at}


@router.delete('/watch-later/{article_id}', response_model=WatchLaterStateOut)
async def remove_from_watch_later(
    article_id: int = Path(gt=0, description='文章 ID。'),
    storage: PostgresStorage = Depends(get_storage),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    """
    把一篇文章移出稍后再看。

    幂等，并且不要求文章仍然可见：退订之后也要能清掉自己留下的条目。

    Returns:
        dict: 文章 ID 与恒为 null 的加入时间。
    """
    async with storage.transaction():
        await storage.watch_later.remove(user_id=user.id, article_pk=article_id)
    return {'article_id': article_id, 'watch_later_at': None}


@router.patch('/watch-later/{article_id}', response_model=WatchLaterMoveOut)
async def move_in_watch_later(
    body: WatchLaterMoveRequest,
    article_id: int = Path(gt=0, description='文章 ID。'),
    storage: PostgresStorage = Depends(get_storage),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    """
    调整一篇稍后再看文章的位置。

    只在我看得见的条目之间移动：看不见的条目不会拦住 `up`/`down`，也不会被挪动。
    已经在最前/最后时是空操作，不报错；文章不在我的可见列表里返回 `404`。

    Args:
        body (WatchLaterMoveRequest): `{"direction": "up" | "down" | "top"}`。

    Returns:
        dict: 文章 ID。
    """
    async with storage.transaction():
        await _move_watch_later(storage, user.id, article_id, body.direction)
    return {'article_id': article_id}


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
        'SELECT c.content_json, a.biz FROM article_content c JOIN articles a ON a.id = c.article_pk'
        ' WHERE c.article_pk = %s',
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

    blocks = await _project_transcript(storage, user.id, row.get('biz'), blocks)
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
        'SELECT a.biz, a.article_id, a.link, a.title, a.author, a.digest, a.cover, a.publish_at, a.raw_json, '
        "COALESCE(acc.source, '') AS account_source"
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
                    if account_source == 'caixin':
                        async with caixin_article_access(thread_storage, owner_article_id):
                            if not await caixin_channel_is_collectable(thread_storage, owner_biz):
                                raise RuntimeError('财新板块已停用，不再重新抓取正文')
                            cookie = await thread_storage.credentials.get(user.id, 'caixin')
                            source = CaixinSource(
                                cookie=cookie or instance_credential(media_settings, 'caixin'),
                                proxy=proxy,
                                pacer=CaixinPacer(),
                            )
                            try:
                                body = await source.fetch_article(str(link))
                            finally:
                                await source.aclose()
                            raw = row.get('raw_json') or {}
                            if isinstance(raw, str):
                                raw = json.loads(raw)
                            article = ArticleRecord(
                                biz=owner_biz,
                                article_id=owner_article_id,
                                title=str(body.get('title') or row.get('title') or ''),
                                author=body.get('author') or row.get('author'),
                                digest=row.get('digest'),
                                cover=row.get('cover'),
                                link=str(link),
                                source_url=str(link),
                                publish_at=row.get('publish_at'),
                                raw=raw,
                            )
                            await ingest_caixin_body(
                                storage=thread_storage,
                                downloader=downloader,
                                article=article,
                                article_pk=article_id,
                                body=body,
                            )
                    else:
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


@router.get('/article/{article_id}/reference', response_model=ArticleReferenceListOut)
async def get_article_references(
    article_id: int,
    storage: PostgresStorage = Depends(get_storage),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    """Read direct WeChat reference states without making upstream requests."""
    return await article_references(storage, user.id, article_id)


@router.post('/article/{article_id}/reference', response_model=ArticleReferenceListOut, status_code=202)
async def fetch_article_references(
    article_id: int,
    body: ArticleReferenceFetchRequest,
    storage: PostgresStorage = Depends(get_storage),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    """Queue missing direct references; only an explicit URL retries a failed job."""
    async with storage.transaction():
        return await article_references(storage, user.id, article_id, enqueue=True, retry_url=body.retry_url)
