"""weixin-rs daemon 数据源：公众号文章列表与正文（替代微信读书来源）。

daemon 跑在 ``/opt/weixin-rs``（config.toml + data + 登录态），本模块只经 RPC 通信：

* 列表 ``get_biz_articles``：给 alias / gh_，拿到长链与元数据。**列表没有短链**，
  所以列表阶段只能入队（``article_queue``），等正文阶段拿到 ``short_link`` 再落
  ``articles``，避免失效长链写进 ``articles.link``。
* 正文 ``get_article_bodies``：给短链或长链（≤50 个），daemon 内部按 ``[articles]``
  分批并自己节流，调用方不需要 sleep。

SDK 位置由 ``WEIXIN_SDK_PATH`` 指定（默认 ``/opt/weixin-rs/sdk/python``）。
"""

from __future__ import annotations

# RPC 超时：daemon 重启 / 网络断掉时，没有超时的调用会永久挂住（曾卡死 2.5 小时）。
LIST_TIMEOUT_SECONDS = 120.0
# 正文要等 daemon 的按篇闸门（还要休息），所以给得宽
BODY_TIMEOUT_SECONDS = 900.0

import base64
import json
import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlparse

from .config import get_settings
from .observability.otel import add_span_attributes, traced, traceparent
from .utils import strip_nul

# 会话类错误（daemon 侧 -13/未登录）：不是文章的错，不计 attempts、整体暂停等重新登录。
# 普通 CGI 的 -13 是会话死；心跳/NewSync 的 -13 由 daemon 的定性阶梯先救，救不回来才到这。
SESSION_MARKERS = ('session expired', '需要重新登录', '需要先登录', '未登录', 'login_qr_start', '[401]')
NETWORK_MARKERS = ('timeout', 'timed out', '超时', '0-RTT', '握手', 'connection', 'Not connected')


class WeixinUnavailableError(RuntimeError):
    """The daemon is unreachable or rejected the call: a source outage, not a bug.

    The container treats this as an outage of one source, so the other sources
    still run in the same round.
    """


class SessionExpiredError(WeixinUnavailableError):
    """daemon 会话失效：调用方应暂停等待登录，不要把它记成文章失败。"""


def classify_daemon_error(message: str) -> str:
    """daemon 错误文本 → 'session' | 'network' | 'upstream'。

    只有 'session' 改变行为（不计 attempts、暂停等登录）；
    其余两类都按可重试计数（到上限为止），区分保留给错误归档。
    """
    text = message or ''
    if any(marker in text for marker in SESSION_MARKERS):
        return 'session'
    if any(marker in text for marker in NETWORK_MARKERS):
        return 'network'
    return 'upstream'


def load_bot_class() -> Any:
    """导入 weixin-rs 的 ``WeChatBot``；路径不对时给出可执行的报错。"""
    sdk_path = get_settings().weixin.sdk_path
    if os.path.isdir(sdk_path) and sdk_path not in sys.path:
        sys.path.insert(0, sdk_path)
    try:
        from weixin_bot import WeChatBot  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError(
            f'weixin-bot SDK 不可用（{sdk_path}）：把 weixin-rs/sdk/python 拷到该路径，或用 WEIXIN_SDK_PATH 指定'
        ) from exc
    return WeChatBot


def load_bot_error_class() -> Any:
    """weixin-rs SDK 的 RPC 错误类型（``WeChatBotError``）。

    Call after :func:`load_bot_class` has put the SDK on ``sys.path``: the SDK is
    optional, so importing it at module load would break hippo without it.
    """
    from weixin_bot import WeChatBotError  # type: ignore[import-not-found]

    return WeChatBotError


def query_param(url: str, key: str) -> str | None:
    """取 URL query 里的参数（长链的 ``sn`` 是列表阶段唯一的稳定标识）。"""
    values = parse_qs(urlparse(url).query).get(key)
    return values[0] if values else None


#: An article's cover is cropped once per display slot (16:9 / 1:1 / 2.35:1 / 3:4),
#: each crop on its own CDN key, and the reader lays cards out in 16:9. The daemon
#: reports every crop as ``cover_urls`` (main first, URLs de-duplicated); the list,
#: the article detail and the push event all carry that same shape.
WIDE_COVER_RATIO = '16:9'
MAIN_COVER_RATIO = 'main'


def select_cover_url(cover_urls: Any, cover_url: Any = None) -> str | None:
    """The 16:9 crop of an article cover, falling back to its main image.

    ``cover_url`` is the daemon's single-image field (the main crop) and all an
    older daemon sends, so it is what remains when no variant fits. Empty entries
    and duplicates carry nothing new and are skipped.
    """
    wide = main = ''
    for variant in cover_urls if isinstance(cover_urls, (list, tuple)) else ():
        if not isinstance(variant, Mapping):
            continue
        url = str(variant.get('url') or '').strip()
        ratio = str(variant.get('ratio') or '').strip()
        if not url:
            continue
        if ratio == WIDE_COVER_RATIO and not wide:
            wide = url
        elif ratio in ('', MAIN_COVER_RATIO) and not main:
            main = url
    return wide or main or str(cover_url or '').strip() or None


@dataclass(slots=True)
class QueuedArticle:
    """列表阶段的一篇文章：只有长链与 ``sn``，短链要等正文阶段。"""

    biz: str
    sn: str
    long_link: str
    appmsg_id: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ListedArticles:
    """一次列表调用的结果：待抓队列项 + daemon 顺带解析出的 ``gh_``（可能为空）。"""

    items: list[QueuedArticle]
    gh_id: str | None = None
    # 下一页游标（服务端签发的不透明串）与「已到最后一页」；历史回填靠它们往后翻。
    next_offset: str | None = None
    is_end: bool = False


@dataclass(frozen=True, slots=True)
class ArticlePublisher:
    """The account an article belongs to, as the article detail reports it.

    A pasted article URL only names a publisher, and the detail response is the
    one place that gives both halves of its identity: ``biz`` (the ``Mz…==``
    fakeid the catalogue is keyed on) and ``gh_id`` (the ``gh_…`` the list RPC
    accepts). Reading it here is what lets a reader subscribe by pasting an
    article, instead of having to search for the account by name.
    """

    biz: str
    gh_id: str
    nickname: str
    alias: str | None = None
    avatar_url: str | None = None


@dataclass(slots=True)
class FetchedArticle:
    """正文阶段的文章：``short_link`` / ``slug`` 已确定，正文即 ``html``。"""

    url: str
    short_link: str
    slug: str
    title: str
    author: str
    digest: str
    user_name: str
    nick_name: str
    publish_time: int
    html: str
    images: list[str]
    cover_url: str | None = None
    source_url: str | None = None
    item_show_type: int | None = None
    raw_json: str = ''

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> FetchedArticle:
        return cls(
            url=d.get('url', ''),
            short_link=d.get('short_link', ''),
            slug=d.get('slug', ''),
            title=d.get('title', ''),
            author=d.get('author', ''),
            digest=d.get('digest', ''),
            user_name=d.get('user_name', ''),
            nick_name=d.get('nick_name', ''),
            publish_time=int(d.get('publish_time') or 0),
            html=d.get('html', ''),
            images=list(d.get('images') or []),
            cover_url=select_cover_url(d.get('cover_urls'), d.get('cover_url')),
            source_url=d.get('source_url'),
            item_show_type=d.get('item_show_type'),
            raw_json=d.get('raw_json', ''),
        )


def publisher_of(body: FetchedArticle) -> ArticlePublisher | None:
    """Read the publisher out of an article's own detail response.

    The daemon exposes the fields downstream needs but not the account identity,
    which stays inside the raw ``Content`` JSON it returns verbatim. Parsing it
    here keeps that knowledge next to the response shape it belongs to, and
    degrades to ``None`` rather than raising: an old daemon, or an article whose
    publisher was deleted, simply has no account to subscribe to.
    """
    try:
        payload = json.loads(body.raw_json or '{}')
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    biz = str(payload.get('bizuin') or '').strip()
    gh_id = str(payload.get('user_name') or '').strip()
    if not biz or not gh_id.startswith('gh_'):
        return None
    alias = str(payload.get('alias') or '').strip() or None
    return ArticlePublisher(
        biz=biz,
        gh_id=gh_id,
        nickname=str(payload.get('nick_name') or body.nick_name or gh_id).strip(),
        alias=alias,
        avatar_url=str(payload.get('round_head_img') or '').strip() or None,
    )


class WeixinSource:
    """daemon RPC 的薄封装：连接、确保登录、列表、正文。"""

    def __init__(self, host: str | None = None, port: int | None = None, *, auto_login: bool = True) -> None:
        weixin = get_settings().weixin
        self.host = host or weixin.daemon_host
        self.port = int(port or weixin.daemon_port)
        self._auto_login = auto_login
        self._bot: Any = None
        self._body_errors: dict[str, str] = {}
        self._bodies_by_key: dict[str, FetchedArticle] = {}

    async def __aenter__(self) -> WeixinSource:
        bot_class = load_bot_class()
        error_class = load_bot_error_class()
        self._bot = bot_class(host=self.host, port=self.port)
        # Carry the active span into each RPC: without this the daemon's server span
        # starts its own trace and the two services never line up.
        self._bot.traceparent = traceparent
        try:
            await self._bot.__aenter__()
            if self._auto_login:
                await self.ensure_login()
        except error_class as exc:
            # An expected RPC/session failure is this source being unavailable, not a
            # Hippo bug: surface the local type so the container isolates it. A
            # CancelledError, a missing SDK or a TypeError is not caught and stays loud.
            raise WeixinUnavailableError(str(exc)) from exc
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._bot is not None:
            await self._bot.__aexit__(*exc)

    async def status(self) -> dict[str, Any]:
        """daemon 的登录/运行状态（不触发登录）。"""
        return await self._bot.get_status()

    async def login_auto_now(self) -> dict[str, Any]:
        """用本地 auto_auth_key 免扫重登（daemon 侧执行）。"""
        return await self._bot.login_auto()

    async def start_qr_login(self) -> Any:
        """索取扫码登录二维码（``LoginQR``，含 png/url/expires_in）。"""
        return await self._bot.login_qr()

    async def wait_login(self, timeout: float = 280.0) -> dict[str, Any]:
        """等扫码确认完成。"""
        return await self._bot.wait_login(timeout=timeout)

    async def ensure_login(self) -> None:
        """daemon 重启后是未登录态，用本地 ```auto_auth_key``` 免扫重登。"""
        status = await self._bot.get_status()
        if status.get('logged_in'):
            return
        if status.get('need_relogin'):
            # 免扫重登已被微信侧拒（``auto_auth_rejected``）：再调 login_auto 只会重复失败，
            # 直接按会话失效上报，调用方会暂停等人工扫码。
            raise SessionExpiredError('daemon 登录态已失效（认证被拒），需要人工扫码重新登录')
        await self._bot.login_auto()
        status = await self._bot.get_status()
        if not status.get('logged_in'):
            raise SessionExpiredError('daemon 未登录，且 login_auto 未成功（需要人工扫码）')

    @traced('daemon.list_articles')
    async def _list_raw(self, source_key: str, pages: int, offset: str = '') -> dict[str, Any]:
        """裸 RPC：响应顶层带着解析后的 ``biz``（``gh_…``），SDK 的高层封装会把它丢掉。"""
        params: dict[str, Any] = {'biz': source_key, 'pages': pages}
        if offset:
            params['offset'] = offset
        res = await self._bot.call('get_biz_articles', params, timeout=LIST_TIMEOUT_SECONDS)
        return res if isinstance(res, dict) else {}

    async def list_articles(
        self,
        source_key: str,
        biz: str,
        pages: int = 1,
        offset: str = '',
    ) -> ListedArticles:
        """按 ``source_key``（微信号 alias 或 gh_）拉列表。

        ``biz`` 是 PG ``accounts.biz``（``Mz…==``），只用于填充队列项——daemon 的列表接口
        不认这个形状。``offset`` 是上一页给出的游标；留空即从最新一页开始。
        """
        res = await self._list_raw(source_key, pages, offset)
        out: list[QueuedArticle] = []
        for article in res.get('articles') or []:
            long_link = str(article.get('canonical_url') or article.get('url') or '')
            sn = query_param(long_link, 'sn')
            if not sn:
                continue
            out.append(
                QueuedArticle(
                    biz=biz,
                    sn=sn,
                    long_link=strip_nul(long_link),
                    payload={
                        'title': strip_nul(article.get('title')),
                        'digest': strip_nul(article.get('digest')),
                        'publish_time': article.get('publish_time'),
                        'cover_url': strip_nul(select_cover_url(article.get('cover_urls'), article.get('cover_url'))),
                    },
                )
            )
        gh_id = str(res.get('biz') or '').strip() or None
        return ListedArticles(
            items=out,
            gh_id=gh_id,
            next_offset=str(res.get('next_offset') or '').strip() or None,
            is_end=bool(res.get('is_end')),
        )

    @traced('daemon.fetch_bodies')
    async def fetch_bodies(self, urls: list[str]) -> list[FetchedArticle]:
        """批量抓正文（短链优先；daemon 负责节流与降级），保留每 URL 的 daemon 诊断。"""
        add_span_attributes({'daemon.batch_size': len(urls)})
        self._body_errors = {}
        self._bodies_by_key = {}
        res = await self._bot.call(
            'get_article_bodies',
            {'urls': list(urls)},
            timeout=BODY_TIMEOUT_SECONDS,
        )
        if not isinstance(res, dict):
            return []
        for error in res.get('errors') or []:
            message = str(error.get('error') or 'daemon returned an unspecified body error')
            for url in error.get('urls') or []:
                self._body_errors[str(url)] = message
        articles = [FetchedArticle.from_dict(body) for body in res.get('articles') or []]
        for article in articles:
            # daemon 回答时可能已经把长链归一化成短链，两个身份都建索引
            for key in (article.url, article.short_link, article.slug, query_param(article.url, 'sn')):
                if key:
                    self._bodies_by_key.setdefault(key, article)
        return articles

    def body_error_for(self, url: str) -> str | None:
        """返回 daemon 对这条 URL 的原始诊断文本；不从错误串推断微信侧原因。"""
        return self._body_errors.get(url)

    def body_for(self, url: str) -> FetchedArticle | None:
        """按任一身份取回正文，避免把 daemon 归一化过的 URL 误判成「没给正文」。"""
        return self._bodies_by_key.get(url) or self._bodies_by_key.get(query_param(url, 'sn') or '')

    async def resolve_article(self, url: str) -> FetchedArticle:
        """Fetch one article's detail, for the account it reveals.

        The detail call is the only token-free way to learn which account a bare
        article URL belongs to. It shares the daemon's body budget, so callers
        treat this as the slow path and only take it when the database cannot
        answer (see ``ArticleRepository.find_biz_by_link``).
        """
        bodies = await self.fetch_bodies([url])
        if bodies:
            return bodies[0]
        raise RuntimeError(self.body_error_for(url) or 'daemon 没有返回这篇文章的详情')

    async def search_public_accounts(self, keyword: str, offset: int = 0) -> list[dict[str, Any]]:
        """搜公众号（daemon `search_biz`）：返回 H5 搜索结果里的 ``gh_`` 对象列表。"""
        return await self._bot.search_biz(keyword, offset=offset)

    async def resolve_fakeid(self, gh_id: str, keyword: str = '') -> str:
        """``gh_…`` → ``__biz``（fakeid）。

        首选该号的文章 URL（``__biz`` 就在长链上），因为它不依赖搜索命中。
        很多号一页文章都拿不到（服务号、刚注册、订阅号列表为空），这时退回 H5
        搜索：搜索结果里那个 ``bizuin`` 的 base64 就是 ``__biz``——用文章长链与
        ``brandInfo`` 里的历史消息链接双向核对过，两者恒等。
        """
        res = await self._list_raw(gh_id, 1)
        for article in res.get('articles') or []:
            for link in (article.get('canonical_url'), article.get('url')):
                biz = query_param(str(link), '__biz') if link else None
                if biz:
                    return biz
        for term in dict.fromkeys(filter(None, (keyword.strip(), gh_id))):
            for hit in await self.search_public_accounts(term):
                if str(hit.get('userName') or '') != gh_id:
                    continue
                bizuin = str(hit.get('bizuin') or '').strip()
                if bizuin:
                    return base64.b64encode(bizuin.encode()).decode()
        raise RuntimeError(f'{gh_id} 既无文章也搜不到，拿不到 __biz')


__all__ = [
    'ArticlePublisher',
    'FetchedArticle',
    'ListedArticles',
    'QueuedArticle',
    'SessionExpiredError',
    'WeixinSource',
    'WeixinUnavailableError',
    'classify_daemon_error',
    'load_bot_class',
    'load_bot_error_class',
    'publisher_of',
    'query_param',
    'select_cover_url',
]
