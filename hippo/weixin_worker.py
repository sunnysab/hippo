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
import logging
from dataclasses import dataclass
from typing import Any

from .downloader import ArticleDownloader
from .models import ArticleRecord
from .storage import PostgresStorage
from .weixin_source import FetchedArticle, WeixinSource

logger = logging.getLogger(__name__)

DOCUMENT_SOURCE = 'api_6771'

# 命中这些特征算可重试（网络/握手/token/服务端 retcode），其余按不可重试处理
_RETRYABLE_MARKERS = ('timeout', '超时', 'retcode', '0-RTT', '握手', 'H5 Session', 'connection')


@dataclass(slots=True)
class SyncStats:
    listed: int = 0
    enqueued: int = 0
    ingested: int = 0
    failed: int = 0

    def merge(self, other: SyncStats) -> None:
        self.listed += other.listed
        self.enqueued += other.enqueued
        self.ingested += other.ingested
        self.failed += other.failed


class WeixinArticleSync:
    """列表入队 + 正文落库；一个实例复用一个 daemon 连接。"""

    def __init__(
        self,
        *,
        storage: PostgresStorage,
        source: WeixinSource,
        downloader: ArticleDownloader,
        batch_size: int = 5,
    ) -> None:
        self._storage = storage
        self._source = source
        self._downloader = downloader
        self._batch_size = max(1, batch_size)

    async def sync_account(self, *, biz: str, source_key: str, pages: int = 1) -> SyncStats:
        """拉一个账号的列表并入队（``source_key`` 是微信号 alias 或 gh_）。"""
        stats = SyncStats()
        listed = await self._source.list_articles(source_key, biz, pages=pages)
        stats.listed = len(listed.items)
        stats.enqueued = self._storage.article_queue.enqueue_many(
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
            self._storage.accounts.set_gh_id(biz, listed.gh_id)
        self._storage.commit()
        logger.info('列表 %s：%d 篇，新入队 %d', source_key, stats.listed, stats.enqueued)
        return stats

    async def drain(self, *, limit: int | None = None) -> SyncStats:
        """把队列里的 pending 抓完（每批 ≤ ``batch_size``）。"""
        stats = SyncStats()
        requeued = self._storage.article_queue.requeue_stale()
        self._storage.commit()
        if requeued:
            logger.warning('重置 %d 条卡在 processing 的队列项', requeued)
        while limit is None or stats.ingested + stats.failed < limit:
            remaining = None if limit is None else limit - stats.ingested - stats.failed
            take = self._batch_size if remaining is None else min(self._batch_size, remaining)
            if take <= 0:
                break
            batch = self._storage.article_queue.take_pending(take)
            self._storage.commit()  # 先落「已领取」，崩溃后由 requeue_stale 兜底
            if not batch:
                break
            await self._process_batch(batch, stats)
        return stats

    async def _process_batch(self, batch: list[dict[str, Any]], stats: SyncStats) -> None:
        urls = [str(row['long_link']) for row in batch]
        try:
            bodies = await self._source.fetch_bodies(urls)
        except Exception as exc:
            self._fail(batch, exc)
            stats.failed += len(batch)
            return
        by_url = {body.url: body for body in bodies}
        for row in batch:
            body = by_url.get(str(row['long_link']))
            if body is None:
                self._fail([row], RuntimeError('响应里没有这一篇'))
                stats.failed += 1
                continue
            try:
                await self._store(row, body)
                self._storage.article_queue.mark_done([int(row['id'])])
                stats.ingested += 1
            except Exception as exc:
                self._storage.rollback()
                logger.warning('落库失败 %s：%s', row.get('long_link'), exc)
                self._fail([row], exc)
                stats.failed += 1
        self._storage.commit()

    def _fail(self, rows: list[dict[str, Any]], exc: Exception) -> None:
        message = str(exc)
        retryable = any(marker in message for marker in _RETRYABLE_MARKERS)
        self._storage.article_queue.mark_failed(
            [int(row['id']) for row in rows], error=message, retryable=retryable
        )
        self._storage.commit()
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
            title=body.title or str(payload.get('title') or '(untitled)'),
            item_show_type=(
                body.item_show_type if body.item_show_type is not None else payload.get('item_show_type')
            ),
            author=body.author or None,
            digest=body.digest or payload.get('digest'),
            cover=body.cover_url or payload.get('cover_url'),
            link=body.short_link,
            source_url=body.source_url,
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
        self._storage.documents.save(
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


__all__ = ['DOCUMENT_SOURCE', 'SyncStats', 'WeixinArticleSync']
