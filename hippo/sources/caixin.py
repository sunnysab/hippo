"""Caixin channels, using the Rust CLI as the only protocol implementation."""

from __future__ import annotations

import asyncio
import json
import re
from contextlib import nullcontext, suppress
from datetime import datetime
from http.cookies import CookieError, SimpleCookie
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from ..config import get_settings
from . import CredentialRejected, SourceError
from .caixin_pacing import CaixinPacer
from .ingest import ChannelInfo, Item, Listing

SOURCE = 'caixin'
REQUEST_TIMEOUT = 45.0
CHANNELS = {
    124: ('www', '财新网'),
    125: ('finance', '金融'),
    126: ('economy', '经济'),
    128: ('opinion', '观点'),
    130: ('companies', '公司'),
    131: ('china', '政经'),
    132: ('international', '世界'),
    160: ('weekly', '周刊'),
}
_HOST_CHANNELS = {f'{host}.caixin.com': channel for channel, (host, _) in CHANNELS.items()}
_ARTICLE_PATH = re.compile(r'/\d{4}-\d{2}-\d{2}/([1-9][0-9]*)\.html')
_SHANGHAI = ZoneInfo('Asia/Shanghai')


class CaixinError(SourceError):
    """A Caixin failure that is safe to show without request credentials."""


class CaixinRateLimited(CaixinError):
    def __init__(self, retry_after_seconds: int) -> None:
        self.retry_after_seconds = retry_after_seconds
        super().__init__(f'财新限制了访问，请在 {retry_after_seconds} 秒后重试')


def _reference(value: str) -> tuple[int, str | None]:
    try:
        parsed = urlsplit(value.strip())
        channel = _HOST_CHANNELS.get((parsed.hostname or '').lower())
        if (
            parsed.scheme not in ('http', 'https')
            or channel is None
            or parsed.netloc.lower() not in _HOST_CHANNELS
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port is not None
            or '\\' in value
            or any(ord(char) < 32 for char in value)
        ):
            raise ValueError
        if parsed.path in ('', '/'):
            return channel, None
        article = _ARTICLE_PATH.fullmatch(parsed.path)
        if article:
            return channel, article[1]
    except ValueError:
        pass
    raise ValueError('Use a supported Caixin channel or article URL')


def _publish_at(value: str) -> int | None:
    try:
        published = datetime.fromisoformat(value.strip())
    except ValueError, AttributeError:
        return None
    if published.tzinfo is None:
        published = published.replace(tzinfo=_SHANGHAI)
    return int(published.timestamp())


class CaixinSource:
    source = SOURCE

    def __init__(
        self,
        *,
        cookie: str = '',
        proxy: str = '',
        binary: str | None = None,
        proxy_config: str | None = None,
        pacer: CaixinPacer | None = None,
    ) -> None:
        self._cookie = cookie
        self._proxy = proxy
        settings = get_settings().caixin
        self._binary = binary if binary is not None else settings.binary
        self._proxy_config = proxy_config if proxy_config is not None else settings.proxy_config
        self._pacer = pacer

    def with_credential(self, cookie: str) -> CaixinSource:
        return CaixinSource(
            cookie=cookie,
            proxy=self._proxy,
            binary=self._binary,
            proxy_config=self._proxy_config,
            pacer=self._pacer,
        )

    def _access(self, kind: str):
        return self._pacer.request(kind) if self._pacer is not None else nullcontext()

    async def aclose(self) -> None:
        pass

    async def preview(self, ref: str) -> ChannelInfo:
        channel, _ = _reference(ref)
        return ChannelInfo(
            biz=f'{SOURCE}:{channel}',
            nickname=f'财新·{CHANNELS[channel][1]}',
            source_key=str(channel),
            source=SOURCE,
        )

    async def _request(self, *args: str) -> Any:
        cookies: SimpleCookie = SimpleCookie()
        try:
            if any(char in self._cookie for char in ('\r', '\n', '\x00')):
                raise CookieError
            cookies.load(self._cookie)
            if self._cookie.strip() and not cookies:
                raise CookieError
        except CookieError:
            raise CaixinError('Invalid Caixin Cookie header') from None
        payload = json.dumps(
            {
                'cookies': {name: item.value for name, item in cookies.items()},
                'proxy': self._proxy or None,
                'request_timeout_secs': 15,
            }
        ).encode()
        try:
            process = await asyncio.create_subprocess_exec(
                self._binary,
                *(['--proxy-config', self._proxy_config] if self._proxy_config else []),
                'fetch',
                *args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except OSError:
            raise CaixinError('Caixin CLI is unavailable; install the caixin binary on PATH') from None
        try:
            stdout, _ = await asyncio.wait_for(process.communicate(payload), timeout=REQUEST_TIMEOUT)
        except (TimeoutError, asyncio.CancelledError) as exc:
            if process.returncode is None:
                with suppress(ProcessLookupError):
                    process.kill()
            await process.wait()
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise CaixinError('Caixin request timed out') from None
        try:
            result = json.loads(stdout)
        except ValueError, UnicodeDecodeError:
            raise CaixinError('Caixin CLI returned invalid JSON') from None
        if not isinstance(result, dict):
            raise CaixinError('Caixin CLI returned an invalid response')
        if result.get('ok') is not True:
            error = result.get('error')
            code = error.get('code') if isinstance(error, dict) else ''
            if not isinstance(code, str):
                code = ''
            if code == 'credential_rejected':
                raise CredentialRejected('Caixin rejected the cookie or subscription permission')
            if code == 'rate_limited':
                seconds = error.get('retry_after_secs', 3600)
                if type(seconds) is not int or seconds < 1:
                    seconds = 3600
                raise CaixinRateLimited(seconds)
            detail = {
                'network': 'Caixin network request failed',
                'network_error': 'Caixin network request failed',
                'parse': 'Caixin response could not be parsed',
                'config': 'Invalid Caixin request configuration',
            }.get(code, 'Caixin request failed')
            raise CaixinError(detail)
        if process.returncode != 0 or 'data' not in result:
            raise CaixinError('Caixin CLI returned an invalid response')
        return result['data']

    async def fetch_article(self, url: str) -> dict[str, Any]:
        """Fetch a validated article; used by listing and explicit refetches."""
        async with self._access('article'):
            return await self._fetch_article(url)

    async def _fetch_article(self, url: str) -> dict[str, Any]:
        _, article_id = _reference(url)
        if article_id is None:
            raise ValueError('A Caixin article URL is required')
        article = await self._request('article', '--url', url)
        if not isinstance(article, dict) or str(article.get('article_id')) != article_id:
            raise CaixinError('Caixin returned a different article')
        content = article.get('content_html')
        if not isinstance(content, str) or not content.strip():
            raise CaixinError('Caixin returned no article body')
        return article

    async def probe(self) -> str:
        """Verify subscription access against a current paid article."""
        # One probe includes discovery and one body, under a single access lease.
        async with self._access('article'):
            return await self._probe()

    async def _probe(self) -> str:
        entries = await self._request('list', '--channel', '132', '--start', '0', '--count', '10')
        if not isinstance(entries, list):
            raise CaixinError('Caixin returned an invalid article list')
        for entry in entries:
            if isinstance(entry, dict) and entry.get('is_paid') is True and isinstance(entry.get('url'), str):
                try:
                    article = await self._fetch_article(entry['url'])
                except ValueError:
                    raise CaixinError('Caixin returned an unsupported article URL') from None
                return str(article.get('title') or entry.get('title') or 'Paid article')
        raise CaixinError('No current paid article was found; subscription access could not be verified')

    async def list_items(self, *, source_key: str, limit: int, cursor: str | None = None) -> Listing:
        async with self._access('list'):
            return await self._list_items(source_key=source_key, limit=limit, cursor=cursor)

    async def _list_items(self, *, source_key: str, limit: int, cursor: str | None = None) -> Listing:
        if source_key not in {str(channel) for channel in CHANNELS}:
            raise ValueError('Unsupported Caixin channel')
        if not 1 <= limit <= 50:
            raise ValueError('Caixin listing limit must be between 1 and 50')
        if cursor is not None and not re.fullmatch(r'[0-9]+', cursor):
            raise ValueError('Invalid Caixin listing cursor')
        start = int(cursor or '0')
        entries = await self._request('list', '--channel', source_key, '--start', str(start), '--count', str(limit))
        if not isinstance(entries, list) or len(entries) > limit:
            raise CaixinError('Caixin returned an invalid article list')
        items: list[Item] = []
        seen: set[str] = set()
        for entry in entries:
            if not isinstance(entry, dict):
                raise CaixinError('Caixin returned an invalid article list')
            article_id = str(entry.get('article_id', ''))
            url = entry.get('url')
            if not re.fullmatch(r'[1-9][0-9]*', article_id) or not isinstance(url, str):
                raise CaixinError('Caixin returned invalid article metadata')
            try:
                _, url_id = _reference(url)
            except ValueError:
                raise CaixinError('Caixin returned an unsupported article URL') from None
            if article_id != url_id:
                raise CaixinError('Caixin article URL does not match its identifier')
            if article_id in seen:
                continue
            seen.add(article_id)
            items.append(
                Item(
                    article_id=article_id,
                    title=str(entry.get('title') or ''),
                    link=url,
                    media_url='',
                    publish_at=_publish_at(entry.get('publish_time', '')),
                    cover=entry.get('image_url') or None,
                    digest=entry.get('summary') or None,
                    author=entry.get('author') or None,
                    raw={'channel_id': int(source_key), 'is_paid': bool(entry.get('is_paid'))},
                )
            )
        is_end = len(entries) < limit
        return Listing(items=items, next_cursor=None if is_end else str(start + len(entries)), is_end=is_end)


__all__ = ['CHANNELS', 'SOURCE', 'CaixinError', 'CaixinSource']
