"""WeRead (i.weread.qq.com) API client for searching accounts and listing articles."""

from __future__ import annotations

import base64
import hashlib
import html
import json
import logging
import secrets
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .config import (
    WEREAD_APP_VERSION,
    WEREAD_BASE_URL,
    WEREAD_BRAND,
    WEREAD_DEFAULT_USER_AGENT,
    WEREAD_SYSTEM_HTTP_AGENT,
)
from .http import MPClient
from .models import ArticleRecord, LoginSession

if TYPE_CHECKING:
    from .storage import PostgresStorage

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
    """Raised when the WeRead access token cannot be used or refreshed."""


def _normalize_article_url(url: str | None) -> str:
    if not url:
        return ''
    value = url.replace('amp;', '').strip()
    if value.startswith('//'):
        value = 'https:' + value
    if value.startswith('http://'):
        value = 'https://' + value[len('http://') :]
    return value


def _to_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    try:
        return int(str(value))
    except ValueError, TypeError:
        return None


def _parse_json(resp: Any, path: str) -> dict[str, Any]:
    try:
        payload = resp.json()
    except (ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(f'Invalid JSON response from {path}') from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f'Unexpected response shape from {path}')
    return payload


@dataclass(slots=True)
class WeChatApiClient:
    """Client for the WeRead (i.weread.qq.com) public-account API."""

    client: MPClient
    storage: PostgresStorage | None = None
    base_url: str = WEREAD_BASE_URL
    user_agent: str = WEREAD_DEFAULT_USER_AGENT
    timeout: float = 20.0

    def _headers(self, credential: LoginSession) -> dict[str, str]:
        return {
            'Accept': 'application/json',
            'User-Agent': self.user_agent,
            'accessToken': credential.access_token,
            'vid': credential.vid,
        }

    async def _refresh(self, credential: LoginSession) -> None:
        if not credential.refresh_token or not credential.device_id:
            raise SessionExpiredError('WeRead session expired; dump has no refresh credentials to renew it')
        timestamp = int(time.time() * 1000)
        random_value = secrets.randbelow(1000)
        body = {
            'refreshToken': credential.refresh_token,
            'deviceId': credential.device_id,
            'wxToken': 0,
            'inBackground': 0,
            'trackId': '',
            'kickType': 1,
            'refCgi': '',
            'timestamp': timestamp,
            'random': random_value,
            'signature': native_signature(
                [credential.device_id, str(timestamp), str(random_value), credential.refresh_token]
            ),
            'virtualChannelId': '',
            'deviceName': 'other',
        }
        resp = await self.client.post(
            f'{self.base_url}/login',
            json=body,
            headers={
                'Accept': 'application/json',
                'Content-Type': 'application/json',
                'User-Agent': self.user_agent,
                'accessToken': credential.access_token,
                'vid': credential.vid,
            },
        )
        payload = _parse_json(resp, '/login refresh')
        errcode = payload.get('errcode')
        if errcode not in (None, 0, '0'):
            message = payload.get('errmsg') or payload.get('errlog') or 'unknown error'
            raise SessionExpiredError(f'WeRead session expired: refresh failed ({errcode}): {message}')
        new_access_token = payload.get('accessToken')
        if not new_access_token:
            raise SessionExpiredError('WeRead session expired: refresh returned no access token')
        credential.access_token = str(new_access_token)
        if payload.get('refreshToken'):
            credential.refresh_token = str(payload['refreshToken'])
        if self.storage is not None:
            try:
                with self.storage.transaction():
                    self.storage.sessions.update_tokens(
                        credential.vid, credential.access_token, credential.refresh_token
                    )
            except Exception as exc:
                logger.warning('Failed to persist refreshed WeRead tokens: %s', exc)

    async def _get(
        self,
        credential: LoginSession,
        path: str,
        params: dict[str, Any],
        *,
        _retry: bool = True,
    ) -> Any:
        query = {key: value for key, value in params.items() if value is not None}
        resp = await self.client.get(
            f'{self.base_url}{path}',
            params=query,
            headers=self._headers(credential),
        )
        status = resp.status_code
        try:
            payload = resp.json()
        except ValueError, json.JSONDecodeError:
            payload = None
        errcode = payload.get('errcode') if isinstance(payload, dict) else None
        if status == 401 or errcode in (-2012, '-2012'):
            if _retry:
                await self._refresh(credential)
                return await self._get(credential, path, params, _retry=False)
            raise SessionExpiredError(f'WeRead session expired (HTTP {status})')
        if isinstance(payload, dict):
            if errcode not in (None, 0, '0'):
                message = payload.get('errmsg') or payload.get('errlog') or 'unknown error'
                raise RuntimeError(f'WeRead API error {errcode}: {message}')
            return payload
        if status >= 400:
            raise RuntimeError(f'HTTP {status} from {path}')
        raise RuntimeError(f'Unexpected response from {path}')

    async def search_public_accounts(
        self,
        credential: LoginSession,
        *,
        keyword: str,
        count: int = 50,
        start: int = 0,
    ) -> dict[str, Any]:
        payload = await self._get(
            credential,
            '/store/search',
            {'keyword': keyword, 'count': count, 'start': start, 'type': 7},
        )
        if not isinstance(payload, dict):
            raise RuntimeError('Unexpected search response shape')
        accounts: list[dict[str, Any]] = []
        for item in payload.get('books', []):
            if not isinstance(item, dict):
                continue
            info = item.get('bookInfo') or {}
            book_id = str(info.get('bookId') or '')
            if not book_id.startswith(_WEREAD_BOOK_ID_PREFIX):
                continue
            accounts.append(
                {
                    'fakeid': book_id_to_biz(book_id),
                    'nickname': info.get('title', '') or '',
                    'alias': info.get('alias', '') or '',
                    'round_head_img': _normalize_article_url(info.get('cover')) or '',
                    'book_id': book_id,
                    'author': info.get('author', '') or '',
                    'intro': info.get('intro', '') or '',
                    'reading_count': item.get('readingCount', 0),
                }
            )
        return {
            'list': accounts,
            'total': payload.get('totalCount') or len(accounts),
            'has_more': bool(payload.get('hasMore')),
        }

    async def list_articles(
        self,
        credential: LoginSession,
        *,
        biz: str,
        offset: int = 0,
        count: int = 20,
    ) -> dict[str, Any]:
        book_id = biz_to_book_id(biz)
        payload = await self._get(
            credential,
            '/mp/chapters',
            {'bookId': book_id, 'count': count, 'offset': offset, 'pf': 'android'},
        )
        if not isinstance(payload, dict):
            raise RuntimeError('Unexpected article response shape')
        return payload


# Parsing helpers -----------------------------------------------------------


def parse_mp_chapters(biz: str, payload: dict[str, Any]) -> list[ArticleRecord]:
    """Convert a WeRead /mp/chapters payload into ArticleRecords.

    *biz* is the canonical MP fakeid; the WeRead book_id is derived from it so
    the stored article_id (review_id) and biz stay consistent with MP-era data.
    """
    book_id = biz_to_book_id(biz)
    items = payload.get('data')
    if not isinstance(items, list):
        items = []
    records: list[ArticleRecord] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        review_id = str(item.get('reviewId') or '')
        if not review_id:
            continue
        link = wechat_article_url(book_id, review_id)
        if not link:
            continue
        mp_info = item.get('mpInfo') or {}
        records.append(
            ArticleRecord(
                biz=biz,
                article_id=review_id,
                title=html.unescape(mp_info.get('title') or '(untitled)'),
                item_show_type=None,
                author=None,
                digest=None,
                cover=_normalize_article_url(mp_info.get('pic_url')) or None,
                link=link,
                source_url=None,
                publish_at=_to_int(item.get('createTime')),
                raw={'reviewId': review_id, 'mpInfo': mp_info, 'createTime': item.get('createTime')},
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
    'parse_mp_chapters',
    'wechat_article_url',
]
