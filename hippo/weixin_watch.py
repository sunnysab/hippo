"""订阅 daemon 的 ``article_push`` 事件，把新文章立刻放进 ``article_queue``。

列表轮询是兜底，推送是实时通道：两条路都往同一个队列写，靠 ``(biz, sn)`` 唯一约束去重，
所以推送重复、乱序、断线补齐都不需要额外处理。

推送里的 ``url`` 是长链（会失效），只当"有新文章"的信号用；入库仍然由正文阶段
走详情接口换成永久短链。
"""

from __future__ import annotations

import asyncio
from typing import Any

from .config import get_settings
from .logger import get_logger
from .storage import PostgresStorage, open_storage
from .weixin_daemon import load_bot_class, query_param, select_cover_url

logger = get_logger(__name__)

RECONNECT_SECONDS = 30.0


def _push_cover_variants(push: Any) -> Any:
    """The cover variants of a push event, however the SDK happens to carry them.

    The event's raw payload is what the daemon sent; the SDK dataclass only grows a
    field once it models one, so both are read before giving up.
    """
    variants = getattr(push, 'cover_urls', None)
    raw = getattr(push, 'raw_data', None)
    if variants is None and isinstance(raw, dict):
        variants = raw.get('cover_urls')
    return variants


async def enqueue_pushed_article(storage: PostgresStorage, push: Any) -> bool:
    """把一条 ``article_push`` 入队，返回是否是新条目。"""
    gh_id = str(getattr(push, 'gh_id', '') or '').strip()
    url = str(getattr(push, 'url', '') or '').strip()
    sn = query_param(url, 'sn') if url else None
    if not gh_id or not sn:
        logger.debug('推送缺少 gh_id/sn，忽略：%s', url[:80])
        return False

    async with storage.conn.cursor() as cur:
        await cur.execute(
            """
            SELECT a.biz FROM accounts a
             WHERE NOT a.is_disabled
               AND (a.gh_id = %s OR a.nickname = %s)
               -- 没人订阅就不再入队，避免浪费 daemon 的抓取配额
               AND EXISTS (
                   SELECT 1 FROM subscription s
                    WHERE s.biz = a.biz AND NOT s.is_disabled
               )
             LIMIT 1
            """,
            (gh_id, getattr(push, 'pub_name', '')),
        )
        row = await cur.fetchone()
    await storage.rollback()
    if row is None:
        logger.debug('推送来自未登记的公众号 %s（%s）', gh_id, getattr(push, 'pub_name', ''))
        return False

    inserted = await storage.article_queue.enqueue_many(
        [
            {
                'biz': str(row[0]),
                'sn': sn,
                'long_link': url,
                'payload': {
                    'title': getattr(push, 'title', ''),
                    'digest': getattr(push, 'digest', ''),
                    # The push event carries the same variants as the list and the
                    # article detail, so the cover rule does not fork per path.
                    'cover_url': select_cover_url(_push_cover_variants(push), getattr(push, 'cover_url', '')),
                    'publish_time': getattr(push, 'publish_time', 0),
                    'gh_id': gh_id,
                    'pub_name': getattr(push, 'pub_name', ''),
                    'source': 'article_push',
                },
            }
        ]
    )
    await storage.commit()
    if inserted:
        logger.info('推送入队：%s - %s', getattr(push, 'pub_name', gh_id), getattr(push, 'title', ''))
    return bool(inserted)


async def watch_article_push(*, reconnect_seconds: float = RECONNECT_SECONDS) -> None:
    """常驻监听公众号推送（断线由 SDK 的 ``start()`` 自行重连）。"""
    bot_class = load_bot_class()
    weixin = get_settings().weixin
    host = weixin.daemon_host
    port = weixin.daemon_port
    while True:
        bot = bot_class(host=host, port=port)

        @bot.on_article
        async def _on_article(push: Any) -> None:
            try:
                async with open_storage() as storage:
                    await enqueue_pushed_article(storage, push)
            except Exception:
                logger.exception('article_push 入队失败')

        try:
            async with bot:
                await bot.start()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception('文章推送通道断开，稍后重连')
        await asyncio.sleep(max(float(reconnect_seconds), 5.0))


__all__ = ['enqueue_pushed_article', 'watch_article_push']
