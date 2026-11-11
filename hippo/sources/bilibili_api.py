"""Bilibili HTTP API and WBI signing, independent of hippo ingestion and media jobs."""

from __future__ import annotations

import re
import time
from hashlib import md5
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode, urlparse

import httpx

from . import CredentialRejected, SourceError

API_BASE = 'https://api.bilibili.com'
USER_AGENT = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36'
REFERER = 'https://www.bilibili.com/'

_BV_RE = re.compile(r'^BV[0-9A-Za-z]{10}$')
_AV_RE = re.compile(r'^av(\d+)$')

#: The permutation Bilibili's WBI signature is built from.
_MIXIN_TABLE = [
    46,
    47,
    18,
    2,
    53,
    8,
    23,
    32,
    15,
    50,
    10,
    31,
    58,
    3,
    45,
    35,
    27,
    43,
    5,
    49,
    33,
    9,
    42,
    19,
    29,
    28,
    14,
    39,
    12,
    38,
    41,
    13,
    37,
    48,
    7,
    16,
    24,
    55,
    40,
    61,
    26,
    17,
    0,
    1,
    60,
    51,
    30,
    4,
    22,
    25,
    54,
    21,
    56,
    59,
    6,
    63,
    57,
    62,
    11,
    36,
    20,
    34,
    44,
    52,
]

_RISK_CONTROL_HINT = 'B站风控拦截：投稿列表需要有效的 SESSDATA（在管理台「媒体与转写」里配置）'


class BilibiliError(SourceError):
    pass


class BilibiliCredentialError(BilibiliError, CredentialRejected):
    """风控拦截 / 未登录：换一份 cookie 重试有意义，网络错误则没有。"""


def mixin_key(img_key: str, sub_key: str) -> str:
    raw = img_key + sub_key
    return ''.join(raw[index] for index in _MIXIN_TABLE if index < len(raw))[:32]


def sign_wbi(params: dict[str, Any], img_key: str, sub_key: str, *, wts: int | None = None) -> dict[str, str]:
    """Sign params the way Bilibili's WBI endpoints expect."""
    signed = {key: str(value) for key, value in params.items()}
    signed['wts'] = str(int(wts if wts is not None else time.time()))
    signed = {
        key: value.translate(str.maketrans({'!': '', "'": '', '(': '', ')': '', '*': ''}))
        for key, value in signed.items()
    }
    query = urlencode(sorted(signed.items()), quote_via=quote, safe='')
    signed['w_rid'] = md5((query + mixin_key(img_key, sub_key)).encode()).hexdigest()
    return signed


class BilibiliClient:
    """The HTTP half. Everything about *what* to fetch lives in the adapter."""

    def __init__(
        self,
        *,
        cookie: str = '',
        proxy: str = '',
        timeout: float = 30.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        headers = {'User-Agent': USER_AGENT, 'Referer': REFERER}
        if cookie:
            headers['Cookie'] = cookie
        self._headers = headers
        self._client = client or httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            proxy=proxy or None,
        )
        self._owns_client = client is None
        self._wbi: tuple[str, str] | None = None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def with_cookie(self, cookie: str) -> BilibiliClient:
        """A view of this client with a different session cookie.

        The connection pool is shared rather than reopened, and the copy does not
        own it: the retry candidates are short-lived, and each of them closing the
        transport would take the others down with it.
        """
        return BilibiliClient(cookie=cookie, client=self._client)

    async def _get_json(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        allow_codes: set[int] | None = None,
    ) -> dict[str, Any]:
        try:
            response = await self._client.get(f'{API_BASE}{path}', params=params, headers=self._headers)
        except httpx.HTTPError as exc:
            raise BilibiliError(f'请求 B站 失败：{exc}') from exc
        if response.status_code in (412, 403):
            raise BilibiliCredentialError(_RISK_CONTROL_HINT)
        if response.status_code != 200:
            raise BilibiliError(f'B站在 {path} 上返回 HTTP {response.status_code}')
        try:
            payload = response.json()
        except ValueError as exc:
            raise BilibiliError(f'B站在 {path} 上返回了非 JSON 内容') from exc
        code = payload.get('code')
        if code in (-352, -412):
            raise BilibiliCredentialError(_RISK_CONTROL_HINT)
        if code in (-101, -111):
            # -101 未登录 / -111 csrf 校验失败：都是「这份 cookie 不顶用」。
            raise BilibiliCredentialError(f'B站接口返回 {code}：需要有效的 SESSDATA')
        if code not in (0, None) and code not in (allow_codes or set()):
            message = payload.get('message') or payload.get('msg') or ''
            raise BilibiliError(f'B站接口返回 {code}：{message}')
        return payload

    async def nav(self) -> dict[str, Any]:
        """The session's own view of itself: logged in, and as whom.

        ``-101`` is not an error here — an anonymous session still gets its WBI keys,
        it just cannot list anyone's uploads.
        """
        return await self._get_json('/x/web-interface/nav', allow_codes={-101})

    async def wbi_keys(self) -> tuple[str, str]:
        if self._wbi:
            return self._wbi
        payload = await self.nav()
        images = ((payload.get('data') or {}).get('wbi_img')) or {}
        img_url = str(images.get('img_url') or '')
        sub_url = str(images.get('sub_url') or '')
        if not img_url or not sub_url:
            raise BilibiliError('B站没有返回 WBI 密钥')
        self._wbi = (Path(urlparse(img_url).path).stem, Path(urlparse(sub_url).path).stem)
        return self._wbi

    async def _get_signed(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        img_key, sub_key = await self.wbi_keys()
        return await self._get_json(path, sign_wbi(params, img_key, sub_key))

    async def video(self, identity: str) -> dict[str, Any]:
        if _BV_RE.match(identity):
            params: dict[str, Any] = {'bvid': identity}
        else:
            matched = _AV_RE.match(identity)
            if not matched:
                raise BilibiliError(f'{identity!r} 不是有效的 BV 或 av 号')
            params = {'aid': matched.group(1)}
        payload = await self._get_json('/x/web-interface/view', params)
        return payload.get('data') or {}

    async def page_list(self, bvid: str) -> list[dict[str, Any]]:
        payload = await self._get_json('/x/player/pagelist', {'bvid': bvid})
        return list(payload.get('data') or [])

    async def player(self, bvid: str, cid: int) -> dict[str, Any]:
        payload = await self._get_signed('/x/player/wbi/v2', {'bvid': bvid, 'cid': cid})
        return payload.get('data') or {}

    async def playurl(self, bvid: str, cid: int) -> dict[str, Any]:
        payload = await self._get_signed(
            '/x/player/wbi/playurl',
            {'bvid': bvid, 'cid': cid, 'fnval': 4048, 'fnver': 0, 'fourk': 1},
        )
        return payload.get('data') or {}

    async def fetch_json(self, url: str) -> dict[str, Any]:
        """Fetch a subtitle file by URL (``subtitle_url`` may arrive protocol-relative)."""
        target = url if url.startswith('http') else f'https:{url}'
        try:
            response = await self._client.get(target, headers=self._headers)
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise BilibiliError(f'字幕文件下载失败：{exc}') from exc

    async def space_videos(self, mid: str, *, page: int, size: int) -> dict[str, Any]:
        payload = await self._get_signed(
            '/x/space/wbi/arc/search',
            {'mid': mid, 'pn': page, 'ps': size, 'order': 'pubdate'},
        )
        return payload.get('data') or {}

    async def season_videos(self, mid: str, season_id: str, *, page: int, size: int) -> dict[str, Any]:
        payload = await self._get_json(
            '/x/polymer/web-space/seasons_archives_list',
            {'mid': mid, 'season_id': season_id, 'page_num': page, 'page_size': size, 'sort_reverse': 'true'},
        )
        return payload.get('data') or {}

    async def space_info(self, mid: str) -> dict[str, Any]:
        payload = await self._get_json('/x/space/acc/info', {'mid': mid})
        return payload.get('data') or {}
