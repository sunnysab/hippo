"""Article and image endpoints."""

import asyncio
import threading
import time as time_module
import uuid
from typing import Any

from fastapi import APIRouter, Depends, Response

from ...article_queries import (
    _block_image,
    _fetch_image,
    _get_article,
    _list_article_images,
    _list_articles,
    _normalize_article_sort,
    _normalize_item_show_type,
    _parse_date,
    _split_article_exclude_keywords,
)
from ...container import build_downloader_container
from ...exceptions import ApiError
from ...logger import get_logger
from ...models import User
from ...storage import PostgresStorage, fetchone_row, open_storage
from ..deps import current_user, get_storage, parse_group_ids
from ..responses import binary_response

logger = get_logger(__name__)
router = APIRouter(dependencies=[Depends(current_user)])

_refetch_tasks: dict[str, dict[str, Any]] = {}
_refetch_lock = threading.Lock()


@router.get('/article')
async def list_articles(
    group_id: int | None = None,
    group_ids: str | None = None,
    biz: str | None = None,
    item_show_type: int | None = None,
    article_id: str | None = None,
    q: str | None = None,
    exclude_keywords: str | None = None,
    sort: str | None = None,
    page: int = 1,
    page_size: int = 20,
    content: str = '',
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
        page (int): 页码。
        page_size (int): 每页数量。
        content (str): 如果为 "1", "true", 或 "yes"，则返回文章内容。
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
    )


@router.get('/article/{article_id}')
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


@router.get('/article/{article_id}/image')
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


@router.get('/image/{image_id}')
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


@router.post('/image/{image_id}/block')
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


@router.get('/article/refetch/{task_id}')
def get_refetch_status(task_id: str) -> dict[str, Any]:
    _cleanup_refetch_tasks()
    with _refetch_lock:
        task = _refetch_tasks.get(task_id)
    if not task:
        raise ApiError('Task not found', status=404)
    return dict(task)


@router.post('/article/{article_id}/refetch')
async def refetch_article(
    article_id: int,
    storage: PostgresStorage = Depends(get_storage),
) -> dict[str, Any]:
    row = await fetchone_row(
        storage,
        'SELECT link FROM articles WHERE id = %s',
        [article_id],
    )
    if not row:
        raise ApiError('Article not found', status=404)
    link = row.get('link')
    if not link:
        raise ApiError('Article has no source URL', status=400)

    task_id = uuid.uuid4().hex
    started_at = time_module.monotonic()
    with _refetch_lock:
        _refetch_tasks[task_id] = {
            'task_id': task_id,
            'status': 'running',
            'phase': 'downloading',
            'started_at': started_at,
            'error': None,
        }
    _cleanup_refetch_tasks()

    def _run() -> None:
        async def _do() -> None:
            async with open_storage() as thread_storage:
                container = build_downloader_container(
                    storage=thread_storage,
                    enable_images=True,
                )
                async with container as app:
                    downloader = app.downloader
                    if not downloader:
                        raise RuntimeError('Downloader not initialized')
                    await downloader.download_from_url(str(link), with_images=True)
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
