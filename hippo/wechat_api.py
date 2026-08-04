"""WeChat business API client built on top of MPClient."""

from __future__ import annotations

import base64
import hashlib
import html
import json
import logging
import time
from collections.abc import Iterable
from dataclasses import dataclass
from http.cookies import SimpleCookie
from typing import Any
from urllib.parse import parse_qs, urlparse

from .config import (
    WEREAD_APP_VERSION,
    WEREAD_BRAND,
    WEREAD_SYSTEM_HTTP_AGENT,
)
from .http import MPClient
from .models import ArticleRecord, LoginSession

logger = logging.getLogger(__name__)


# --- WeRead primitives -------------------------------------------------------

# The 10.2.1 APK's native Remap table used by EncryptUtils.nativeGetSignatures.
SIGNATURE_REMAP_TABLE = bytes.fromhex(
    '34ca55401db693c63130293532a7b811c2b516fa8bb124a4109004e908f83b8a'
    '9c8c44f9bc5c69e2a1dad2d37589f71e2d5056d77253bf22fb200f012e45876e'
    '6648f2e0cdfe67a943f49451cea54aee13268eccaa33145d0e39bbcf912b814d'
    'ea99ec1a2c85c5d936744b18e1f13d9d419fb4170dd64cbedcaf972877f062ff'
    '71c1c8278f6c68a89be6591c1b1209984e3f063700ba1f0a192fc9d5d057496f'
    'fd25e4610c42cb96645fdbad60238d9a6dc3c45e3eb9926abd5b077f7695ed4f'
    'ab847a80e778c7e5eb73836bfc38467d4765b352633a05d1efa3a6de9e3c02ae'
    'b27ba0f6f32ac0ac86035a540bf582d47ee3dfb0d8dd21e87c88a2795870b715'
)
SIGNATURE_SALT = b'5a6f1'

_WEREAD_BOOK_ID_PREFIX = 'MP_WXS_'


def native_signature(values: list[str]) -> str:
    """Reproduce EncryptUtils.nativeGetSignatures from the 10.2.1 APK."""

    parts = [bytes(SIGNATURE_REMAP_TABLE[byte] for byte in value.encode()) for value in values]
    parts.append(SIGNATURE_SALT)
    data = b''.join(sorted(parts))

    def digest_rotated(value: bytes) -> bytes:
        checksum = 0
        for byte in value:
            checksum ^= byte
        shift = checksum % 11
        rotated = bytearray(len(value))
        for index, byte in enumerate(value):
            rotated[(shift + index) % len(value)] = byte
        return hashlib.sha256(rotated).hexdigest().encode()

    return digest_rotated(digest_rotated(data)).decode()


def build_user_agent(
    app_version: str = WEREAD_APP_VERSION,
    brand: str = WEREAD_BRAND,
    system_http_agent: str = WEREAD_SYSTEM_HTTP_AGENT,
) -> str:
    return f'WeRead/{app_version} WRBrand/{brand} {system_http_agent}'


def _b64decode_str(value: str) -> str:
    core = value.rstrip('=')
    padding = '=' * (-len(core) % 4)
    return base64.b64decode(core + padding).decode('utf-8')


def biz_to_book_id(biz: str) -> str:
    """Map a WeChat MP fakeid (biz) to a WeRead public-account book_id.

    The MP fakeid is the base64 encoding of the numeric id that follows the
    ``MP_WXS_`` prefix in the WeRead book_id. Values already shaped like a
    book_id are returned unchanged.
    """
    if biz.startswith(_WEREAD_BOOK_ID_PREFIX):
        return biz
    try:
        decoded = _b64decode_str(biz)
    except ValueError, UnicodeDecodeError:
        return biz
    return f'{_WEREAD_BOOK_ID_PREFIX}{decoded}'


def book_id_to_biz(book_id: str) -> str:
    """Map a WeRead book_id back to the canonical MP fakeid (biz)."""
    if not book_id.startswith(_WEREAD_BOOK_ID_PREFIX):
        return book_id
    numeric = book_id[len(_WEREAD_BOOK_ID_PREFIX) :]
    return base64.b64encode(numeric.encode()).decode()


def wechat_article_url(book_id: str, review_id: str) -> str | None:
    """Derive the canonical ``mp.weixin.qq.com/s/{token}`` URL from a review_id."""
    prefix = f'{book_id}_'
    if not review_id.startswith(prefix):
        return None
    article_id = review_id[len(prefix) :]
    return f'https://mp.weixin.qq.com/s/{article_id}' if article_id else None


class SessionExpiredError(RuntimeError):
    pass


def _is_session_error_message(message: str) -> bool:
    lowered = message.lower()
    hints = ('invalid session', 'invalid token', 'session expired', 'session timeout', 'expired')
    return any(hint in lowered for hint in hints)


def _raise_for_base_resp(payload: dict[str, Any], *, fallback: str) -> None:
    base_resp = payload.get('base_resp') or {}
    if base_resp.get('ret') == 0:
        return
    err_msg = str(base_resp.get('err_msg') or fallback)
    if _is_session_error_message(err_msg):
        raise SessionExpiredError(err_msg)
    raise RuntimeError(err_msg)


@dataclass(slots=True)
class WeChatApiClient:
    client: MPClient

    async def start_login_session(self, sid: str) -> str:
        payload = {
            'userlang': 'zh_CN',
            'redirect_url': '',
            'login_type': 3,
            'sessionid': sid,
            'token': '',
            'lang': 'zh_CN',
            'f': 'json',
            'ajax': 1,
        }
        resp = await self.client.post(
            'https://mp.weixin.qq.com/cgi-bin/bizlogin',
            params={'action': 'startlogin'},
            data=payload,
        )
        resp.raise_for_status()
        cookies = _parse_set_cookies(resp.headers.get_list('set-cookie'))
        uuid = cookies.get('uuid')
        if not uuid:
            raise RuntimeError('Failed to get login uuid cookie.')
        return f'uuid={uuid}'

    async def fetch_login_qrcode(self, cookie: str) -> bytes:
        resp = await self.client.get(
            'https://mp.weixin.qq.com/cgi-bin/scanloginqrcode',
            params={'action': 'getqrcode', 'random': int(time.time() * 1000)},
            headers={'Cookie': cookie},
        )
        resp.raise_for_status()
        return resp.content

    async def check_login_status(self, cookie: str) -> dict[str, Any]:
        resp = await self.client.get(
            'https://mp.weixin.qq.com/cgi-bin/scanloginqrcode',
            params={'action': 'ask', 'token': '', 'lang': 'zh_CN', 'f': 'json', 'ajax': 1},
            headers={'Cookie': cookie},
        )
        resp.raise_for_status()
        return resp.json()

    async def finalize_login(self, cookie: str) -> LoginSession:
        payload = {
            'userlang': 'zh_CN',
            'redirect_url': '',
            'cookie_forbidden': 0,
            'cookie_cleaned': 0,
            'plugin_used': 0,
            'login_type': 3,
            'token': '',
            'lang': 'zh_CN',
            'f': 'json',
            'ajax': 1,
        }
        resp = await self.client.post(
            'https://mp.weixin.qq.com/cgi-bin/bizlogin',
            params={'action': 'login'},
            data=payload,
            headers={'Cookie': cookie},
        )
        resp.raise_for_status()
        payload_json = resp.json()
        logger.info('finalize_login response: %s', json.dumps(payload_json, ensure_ascii=False))
        redirect_url = payload_json.get('redirect_url') or ''
        if not redirect_url:
            raise RuntimeError(f'Login failed: missing redirect_url, response keys: {list(payload_json.keys())}')
        token = _extract_token(redirect_url)
        if not token:
            raise RuntimeError('Login failed: missing token')
        cookies = _parse_set_cookies(resp.headers.get_list('set-cookie'))
        return LoginSession(token=token, cookies=cookies)

    async def fetch_login_info(self, session: LoginSession) -> dict[str, str]:
        resp = await self.client.get(
            'https://mp.weixin.qq.com/cgi-bin/home',
            params={'t': 'home/index', 'token': session.token, 'lang': 'zh_CN'},
            headers={'Cookie': _cookie_header(session.cookies)},
        )
        html = resp.text
        nickname = _match_value(html, r'wx\.cgiData\.nick_name\s*?=\s*?"(?P<value>[^"]+)"')
        avatar = _match_value(html, r'wx\.cgiData\.head_img\s*?=\s*?"(?P<value>[^"]+)"')
        return {'nickname': nickname or '', 'avatar': avatar or ''}

    async def search_biz(
        self,
        session: LoginSession,
        *,
        keyword: str,
        begin: int = 0,
        count: int = 5,
    ) -> dict[str, Any]:
        params = {
            'action': 'search_biz',
            'begin': begin,
            'count': count,
            'query': keyword,
            'token': session.token,
            'lang': 'zh_CN',
            'f': 'json',
            'ajax': '1',
        }
        resp = await self.client.get(
            'https://mp.weixin.qq.com/cgi-bin/searchbiz',
            params=params,
            headers={'Cookie': _cookie_header(session.cookies)},
        )
        resp.raise_for_status()
        payload = resp.json()
        _raise_for_base_resp(payload, fallback='searchbiz failed')
        return payload

    async def fetch_appmsg_publish(
        self,
        session: LoginSession,
        *,
        fakeid: str,
        begin: int = 0,
        count: int = 5,
        keyword: str = '',
    ) -> dict[str, Any]:
        is_searching = bool(keyword)
        params = {
            'sub': 'search' if is_searching else 'list',
            'search_field': '7' if is_searching else 'null',
            'begin': begin,
            'count': count,
            'query': keyword,
            'fakeid': fakeid,
            'type': '101_1',
            'free_publish_type': 1,
            'sub_action': 'list_ex',
            'token': session.token,
            'lang': 'zh_CN',
            'f': 'json',
            'ajax': 1,
        }
        resp = await self.client.get(
            'https://mp.weixin.qq.com/cgi-bin/appmsgpublish',
            params=params,
            headers={'Cookie': _cookie_header(session.cookies)},
        )
        resp.raise_for_status()
        payload = resp.json()
        _raise_for_base_resp(payload, fallback='appmsgpublish failed')
        return payload


# Parsing helpers -----------------------------------------------------------


def _parse_general_msg_list(raw: str) -> list[dict]:
    if not raw:
        return []
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError('Failed to parse general_msg_list') from exc
    return payload.get('list') or []


def _normalize_article_url(url: str | None) -> str:
    if not url:
        return ''
    value = url.replace('amp;', '').strip()
    if value.startswith('//'):
        value = 'https:' + value
    if value.startswith('http://'):
        value = 'https://' + value[len('http://') :]
    return value


def _parse_set_cookies(set_cookies: list[str]) -> dict[str, str]:
    jar: dict[str, str] = {}
    for item in set_cookies:
        cookie = SimpleCookie()
        cookie.load(item)
        for name, morsel in cookie.items():
            if morsel.value and morsel.value != 'EXPIRED':
                jar[name] = morsel.value
    return jar


def _cookie_header(cookies: dict[str, str]) -> str:
    return '; '.join(f'{k}={v}' for k, v in cookies.items())


def _extract_token(redirect_url: str) -> str:
    parsed = urlparse(redirect_url)
    qs = parse_qs(parsed.query)
    return (qs.get('token') or [''])[0]


def _match_value(html: str, pattern: str) -> str:
    import re

    match = re.search(pattern, html)
    if match and match.groupdict().get('value'):
        return match.group('value')
    return ''


def _message_to_articles(biz: str, message: dict) -> Iterable[ArticleRecord]:
    comm_info = message.get('comm_msg_info') or {}
    ext_info = message.get('app_msg_ext_info') or {}
    if not ext_info:
        return []

    records: list[ArticleRecord] = []
    primary = _build_article(biz, comm_info, ext_info, index=0)
    if primary:
        records.append(primary)
    if ext_info.get('is_multi'):
        for idx, item in enumerate(ext_info.get('multi_app_msg_item_list') or [], start=1):
            record = _build_article(biz, comm_info, item, index=idx)
            if record:
                records.append(record)
    return records


def _build_article(biz: str, comm: dict, item: dict, *, index: int) -> ArticleRecord | None:
    link = _normalize_article_url(item.get('content_url'))
    if not link:
        return None
    publish_at = comm.get('datetime')
    article_id = f'{comm.get("id")}-{index}'
    raw = {
        'comm_msg_info': comm,
        'app_msg_ext_info': item,
    }
    return ArticleRecord(
        biz=biz,
        article_id=article_id,
        title=html.unescape(item.get('title') or '(untitled)'),
        item_show_type=item.get('item_show_type'),
        author=html.unescape(item.get('author') or '') or None,
        digest=html.unescape(item.get('digest') or '') or None,
        cover=_normalize_article_url(item.get('cover')),
        link=link,
        source_url=_normalize_article_url(item.get('source_url')),
        publish_at=publish_at,
        raw=raw,
    )


def parse_appmsg_publish(fakeid: str, payload: dict[str, Any]) -> list[ArticleRecord]:
    publish_page = {}
    raw_page = payload.get('publish_page') or '{}'
    try:
        publish_page = json.loads(raw_page)
    except json.JSONDecodeError:
        publish_page = {}
    publish_list = publish_page.get('publish_list') or []
    records: list[ArticleRecord] = []
    for item in publish_list:
        info_raw = item.get('publish_info')
        if not info_raw:
            continue
        try:
            info = json.loads(info_raw)
        except json.JSONDecodeError:
            continue
        for appmsg in info.get('appmsgex') or []:
            if appmsg.get('is_deleted'):
                continue
            link = _normalize_article_url(appmsg.get('link'))
            if not link:
                continue
            publish_at = appmsg.get('update_time') or appmsg.get('create_time')
            article_id = f'{appmsg.get("appmsgid")}-{appmsg.get("itemidx")}'
            records.append(
                ArticleRecord(
                    biz=fakeid,
                    article_id=article_id,
                    title=html.unescape(appmsg.get('title') or '(untitled)'),
                    item_show_type=appmsg.get('item_show_type'),
                    author=html.unescape(appmsg.get('author_name') or '') or None,
                    digest=html.unescape(appmsg.get('digest') or '') or None,
                    cover=_normalize_article_url(appmsg.get('cover') or appmsg.get('cover_img')),
                    link=link,
                    source_url=None,
                    publish_at=publish_at,
                    raw={'appmsgex': appmsg},
                )
            )
    return records


__all__ = [
    'SessionExpiredError',
    'WeChatApiClient',
    'biz_to_book_id',
    'book_id_to_biz',
    'build_user_agent',
    'native_signature',
    'parse_appmsg_publish',
    'wechat_article_url',
]
