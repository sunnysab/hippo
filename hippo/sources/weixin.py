"""The WeChat adapter.

``WeixinArticleSync`` already is the contract (list into a queue, then drain the
bodies), so this is only a name that the registry can key on.
"""

from __future__ import annotations

from ..sync_types import SyncStats
from ..weixin_ingest import WeixinArticleSync

#: ``accounts.source`` 上的取值：回填只有微信这条路径，靠它把别的源挡在外面。
SOURCE = 'weixin'


class WeixinSourceAdapter:
    def __init__(self, sync: WeixinArticleSync) -> None:
        self._sync = sync

    @property
    def sync(self) -> WeixinArticleSync:
        return self._sync

    async def sync_account(
        self,
        *,
        biz: str,
        source_key: str,
        pages: int = 1,
        offset: str = '',
    ) -> SyncStats:
        return await self._sync.sync_account(biz=biz, source_key=source_key, pages=pages, offset=offset)

    async def drain(self, *, limit: int | None = None) -> SyncStats:
        return await self._sync.drain(limit=limit)


__all__ = ['SOURCE', 'WeixinSourceAdapter']
