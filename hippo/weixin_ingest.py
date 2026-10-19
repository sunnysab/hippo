"""weixin-rs 数据源 worker：列表入队 + 正文落库。

与 hippo 原有的「微信读书列表 + 网页抓正文」不同，这里：

* 列表 ``get_biz_articles`` 只给长链（会失效），所以先入 ``article_queue``；
* 正文 ``get_article_bodies`` 给出永久短链 ``short_link``，才写 ``articles``
  （``article_id = slug``、``link = 短链``）；
* 原始正文写 ``article_document``，派生内容（markdown + blocks）写 ``article_content``；
* 抓取节流（5~10 篇/分钟 + 定期休息）在 daemon 侧，worker 只分批调用。
"""

from __future__ import annotations

import json
from typing import Any

from .downloader import ArticleDownloader
from .logger import get_logger
from .models import ArticleRecord
from .storage import PostgresStorage
from .sync_types import SyncStats
from .utils import strip_nul
from .weixin_daemon import FetchedArticle, SessionExpiredError, WeixinSource, classify_daemon_error

logger = get_logger(__name__)

DOCUMENT_SOURCE = 'api_6771'


def _split_batch_error(message: str) -> dict[str, str]:
    """拆开老 daemon 的整批错误：``<url>: <原因>; <url>: <原因>``。

    daemon 现在每条 error 只挂一个 URL，这里已经用不上了；留着是为了兼容还没升级的
    daemon——那时 ``fetch_bodies`` 整体报错、每行拿到同一段拼接文本，从里面认出自己的
    URL 才能把原因记到对应队列项上（否则整批共用一条，还被 1000 字符截断丢掉尾部）。
    格式对不上就返回空字典，调用方退回整段文本。
    """
    reasons: dict[str, str] = {}
    for part in message.split('; '):
        url, sep, reason = part.partition(': ')
        if sep and url.startswith('http'):
            reasons[url] = reason
    return reasons


class WeixinArticleSync:
    """列表入队 + 正文落库；一个实例复用一个 daemon 连接。"""

    def __init__(
        self,
        *,
        storage: PostgresStorage,
        source: WeixinSource,
        downloader: ArticleDownloader | None = None,
        batch_size: int = 5,
    ) -> None:
        self._storage = storage
        self._source = source
        self._downloader = downloader
        self._batch_size = max(1, batch_size)

    async def sync_account(
        self,
        *,
        biz: str,
        source_key: str,
        pages: int = 1,
        offset: str = '',
    ) -> SyncStats:
        """拉一个账号的一页（或多页）列表并入队（``source_key`` 是微信号 alias 或 gh_）。"""
        stats = SyncStats()
        listed = await self._source.list_articles(source_key, biz, pages=pages, offset=offset)
        stats.listed = len(listed.items)
        stats.next_offset = listed.next_offset
        stats.is_end = listed.is_end
        stats.enqueued = await self._storage.article_queue.enqueue_many(
            {
                'biz': item.biz,
                'sn': item.sn,
                'appmsg_id': item.appmsg_id,
                'long_link': item.long_link,
                'payload': item.payload,
            }
            for item in listed.items
        )
        # 列表响应顺带带回了 daemon 解析出的 gh_：缓存下来，下次解析就不必再走 searchcontact
        if listed.gh_id:
            await self._storage.accounts.set_gh_id(biz, listed.gh_id)
        await self._storage.commit()
        logger.info('列表 %s：%d 篇，新入队 %d', source_key, stats.listed, stats.enqueued)
        return stats

    async def drain(self, *, limit: int | None = None) -> SyncStats:
        """把队列里的 pending 抓完（每批 ≤ ``batch_size``）。"""
        if self._downloader is None:
            raise RuntimeError('这次实例没有 downloader，不能抓正文')
        stats = SyncStats()
        requeued = await self._storage.article_queue.requeue_stale()
        await self._storage.commit()
        if requeued:
            logger.warning('重置 %d 条卡在 processing 的队列项', requeued)
        while limit is None or stats.ingested + stats.failed < limit:
            remaining = None if limit is None else limit - stats.ingested - stats.failed
            take = self._batch_size if remaining is None else min(self._batch_size, remaining)
            if take <= 0:
                break
            batch = await self._storage.article_queue.take_pending(take)
            await self._storage.commit()  # 先落「已领取」，崩溃后由 requeue_stale 兜底
            if not batch:
                break
            await self._process_batch(batch, stats)
        return stats

    async def _process_batch(self, batch: list[dict[str, Any]], stats: SyncStats) -> None:
        try:
            await self._process_batch_inner(batch, stats)
        except SessionExpiredError:
            # 会话失效是基础设施故障：整批回 pending 且不计 attempts，交给上层等重新登录。
            await self._storage.article_queue.requeue([int(row['id']) for row in batch], error='daemon session expired')
            await self._storage.commit()
            raise

    async def _process_batch_inner(self, batch: list[dict[str, Any]], stats: SyncStats) -> None:
        urls = [str(row['long_link']) for row in batch]
        try:
            await self._source.fetch_bodies(urls)
        except Exception as exc:
            message = str(exc)
            if classify_daemon_error(message) == 'session':
                await self._fail(batch, exc)
                return
            reasons = _split_batch_error(message)
            for row in batch:
                await self._fail([row], RuntimeError(reasons.get(str(row['long_link'])) or message))
            stats.failed += len(batch)
            return
        for row in batch:
            url = str(row['long_link'])
            body = self._source.body_for(url)
            if body is None or not body.html:
                diagnostic = self._source.body_error_for(url) or self._missing_body_reason(url, body)
                await self._fail([row], RuntimeError(diagnostic))
                stats.failed += 1
                continue
            try:
                await self._store(row, body)
                await self._storage.article_queue.mark_done([int(row['id'])])
                stats.ingested += 1
            except Exception as exc:
                await self._storage.rollback()
                logger.warning('落库失败 %s：%s', row.get('long_link'), exc)
                await self._fail([row], exc)
                stats.failed += 1
        await self._storage.commit()

    @staticmethod
    def _missing_body_reason(url: str, body: FetchedArticle | None) -> str:
        """daemon 既没给 html 也没报错时，把能确定的事实写清楚，便于事后判因。"""
        if body is None:
            return f'daemon returned no article entry for {url}'
        return (
            f'daemon returned an empty body for {body.short_link or url}'
            f' (title={body.title!r}, slug={body.slug!r}, images={len(body.images)})'
        )

    async def _fail(self, rows: list[dict[str, Any]], exc: Exception) -> None:
        message = str(exc)
        if classify_daemon_error(message) == 'session':
            # 不写 attempts、不写 failed：让 _process_batch 整批回 pending
            raise SessionExpiredError(message)
        # We do not infer upstream permanence from our own parser/error text.
        # Retry is bounded by article_queue.mark_failed (three attempts), while the
        # original daemon diagnostic remains recorded for inspection.
        retryable = True
        await self._storage.article_queue.mark_failed(
            [int(row['id']) for row in rows], error=message, retryable=retryable
        )
        await self._storage.commit()
        logger.warning('抓正文失败（retryable=%s）：%s', retryable, message)

    async def _store(self, row: dict[str, Any], body: FetchedArticle) -> None:
        if not body.short_link or not body.slug:
            raise RuntimeError('响应没有 short_link，无法确定永久链接')
        payload = row.get('payload') or {}
        if isinstance(payload, str):
            payload = json.loads(payload)
        article = ArticleRecord(
            biz=str(row['biz']),
            article_id=body.slug,
            title=strip_nul(body.title or str(payload.get('title') or '(untitled)')),
            item_show_type=(body.item_show_type if body.item_show_type is not None else payload.get('item_show_type')),
            author=strip_nul(body.author) or None,
            digest=strip_nul(body.digest or payload.get('digest')),
            cover=strip_nul(body.cover_url or payload.get('cover_url')),
            link=strip_nul(body.short_link),
            source_url=strip_nul(body.source_url),
            publish_at=body.publish_time or payload.get('publish_time'),
            raw={'sn': row['sn'], 'list': payload},
        )
        # with_images=False：图片只登记进 article_images，由独立的回填循环去抓，
        # 避免正文（客户端协议）与图片（普通 HTTPS CDN）互相拖累、也没了两条独立的节奏。
        article_pk = await self._downloader.ingest_body(
            article=article,
            html=body.html,
            title=article.title,
            item_show_type=article.item_show_type,
            with_images=False,
        )
        if article_pk is None:
            raise RuntimeError('articles 写入后没有拿到 id')
        await self._storage.documents.save(
            article_pk=article_pk,
            source=DOCUMENT_SOURCE,
            url_token=body.slug,
            raw_html=body.html,
            raw_json=_parse_raw_json(body.raw_json),
        )


def _parse_raw_json(raw: str) -> Any | None:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


__all__ = ['DOCUMENT_SOURCE', 'WeixinArticleSync']
