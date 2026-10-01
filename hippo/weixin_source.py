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

import os
import sys
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlparse

DEFAULT_SDK_PATH = '/opt/weixin-rs/sdk/python'

# 会话类错误（daemon 侧 -13/未登录）：不是文章的错，不计 attempts、整体暂停等重新登录。
# 普通 CGI 的 -13 是会话死；心跳/NewSync 的 -13 由 daemon 的定性阶梯先救，救不回来才到这。
SESSION_MARKERS = ('session expired', '需要重新登录', '需要先登录', '未登录', 'login_qr_start', '[401]')
NETWORK_MARKERS = ('timeout', 'timed out', '超时', '0-RTT', '握手', 'connection', 'Not connected')


class SessionExpiredError(RuntimeError):
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
    sdk_path = os.environ.get('WEIXIN_SDK_PATH') or DEFAULT_SDK_PATH
    if os.path.isdir(sdk_path) and sdk_path not in sys.path:
        sys.path.insert(0, sdk_path)
    try:
        from weixin_bot import WeChatBot  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError(
            f'weixin-bot SDK 不可用（{sdk_path}）：把 weixin-rs/sdk/python 拷到该路径，或用 WEIXIN_SDK_PATH 指定'
        ) from exc
    return WeChatBot


def query_param(url: str, key: str) -> str | None:
    """取 URL query 里的参数（长链的 ``sn`` 是列表阶段唯一的稳定标识）。"""
    values = parse_qs(urlparse(url).query).get(key)
    return values[0] if values else None


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
            cover_url=d.get('cover_url'),
            source_url=d.get('source_url'),
            item_show_type=d.get('item_show_type'),
            raw_json=d.get('raw_json', ''),
        )


class WeixinSource:
    """daemon RPC 的薄封装：连接、确保登录、列表、正文。"""

    def __init__(self, host: str | None = None, port: int | None = None, *, auto_login: bool = True) -> None:
        self.host = host or os.environ.get('WEIXIN_DAEMON_HOST', '127.0.0.1')
        self.port = int(port or os.environ.get('WEIXIN_DAEMON_PORT', '9099'))
        self._auto_login = auto_login
        self._bot: Any = None
        self._body_errors: dict[str, str] = {}

    async def __aenter__(self) -> WeixinSource:
        bot_class = load_bot_class()
        self._bot = bot_class(host=self.host, port=self.port)
        await self._bot.__aenter__()
        if self._auto_login:
            await self.ensure_login()
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

    async def _list_raw(self, source_key: str, pages: int) -> dict[str, Any]:
        """裸 RPC：响应顶层带着解析后的 ``biz``（``gh_…``），SDK 的高层封装会把它丢掉。"""
        res = await self._bot.call(
            'get_biz_articles', {'biz': source_key, 'pages': pages}, timeout=LIST_TIMEOUT_SECONDS
        )
        return res if isinstance(res, dict) else {}

    async def list_articles(self, source_key: str, biz: str, pages: int = 1) -> ListedArticles:
        """按 ``source_key``（微信号 alias 或 gh_）拉列表。

        ``biz`` 是 PG ``accounts.biz``（``Mz…==``），只用于填充队列项——daemon 的列表接口
        不认这个形状。
        """
        res = await self._list_raw(source_key, pages)
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
                    long_link=long_link,
                    payload={
                        'title': article.get('title'),
                        'digest': article.get('digest'),
                        'publish_time': article.get('publish_time'),
                        'cover_url': article.get('cover_url'),
                    },
                )
            )
        gh_id = str(res.get('biz') or '').strip() or None
        return ListedArticles(items=out, gh_id=gh_id)

    async def fetch_bodies(self, urls: list[str]) -> list[FetchedArticle]:
        """批量抓正文（短链优先；daemon 负责节流与降级），保留每 URL 的 daemon 诊断。"""
        self._body_errors = {}
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
        return [FetchedArticle.from_dict(body) for body in res.get('articles') or []]

    def body_error_for(self, url: str) -> str | None:
        """返回 daemon 对这条 URL 的原始诊断文本；不从错误串推断微信侧原因。"""
        return self._body_errors.get(url)

    async def search_public_accounts(self, keyword: str, offset: int = 0) -> list[dict[str, Any]]:
        """搜公众号（daemon `search_biz`）：返回 H5 搜索结果里的 ``gh_`` 对象列表。"""
        return await self._bot.search_biz(keyword, offset=offset)

    async def resolve_fakeid(self, gh_id: str) -> str:
        """``gh_…`` → ``__biz``（fakeid）：抓一页列表，从文章 URL 里取。

        ``accounts.biz`` 全库都是 fakeid（``Mz…==``），而 daemon 的搜索只给 gh_，
        所以落库前必须先拿到这个号任意一篇文章的 URL（未关注的号可能拿不到）。
        """
        res = await self._list_raw(gh_id, 1)
        for article in res.get('articles') or []:
            for link in (article.get('canonical_url'), article.get('url')):
                biz = query_param(str(link), '__biz') if link else None
                if biz:
                    return biz
        raise RuntimeError(f'{gh_id} 暂无可用文章，拿不到 __biz（可能需要先关注）')


__all__ = [
    'FetchedArticle',
    'ListedArticles',
    'QueuedArticle',
    'SessionExpiredError',
    'WeixinSource',
    'classify_daemon_error',
    'load_bot_class',
    'query_param',
]
