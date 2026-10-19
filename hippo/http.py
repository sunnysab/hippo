"""HTTP helpers for interacting with WeChat endpoints."""

from __future__ import annotations

import ssl
from contextlib import AbstractAsyncContextManager
from typing import Any
from urllib.parse import quote, urlparse

import certifi
import httpx

from .config import DEFAULT_USER_AGENT, get_settings
from .logger import get_logger

logger = get_logger(__name__)


class ArticleContentUnavailableError(RuntimeError):
    pass


HEADERS = {
    'User-Agent': DEFAULT_USER_AGENT,
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
    'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
    'Accept-Encoding': 'gzip, deflate, br',
    'Cache-Control': 'max-age=0',
    'Sec-Fetch-Dest': 'document',
    'Sec-Fetch-Mode': 'navigate',
    'Sec-Fetch-Site': 'none',
    'Sec-Fetch-User': '?1',
    'Upgrade-Insecure-Requests': '1',
    'Referer': 'https://mp.weixin.qq.com/',
    'Origin': 'https://mp.weixin.qq.com',
}


class WorkerURLTransport(httpx.AsyncBaseTransport):
    def __init__(self, base: httpx.AsyncBaseTransport, worker_url: str | None) -> None:
        self._base = base
        self._worker_url = worker_url.rstrip('/') if worker_url else None

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if self._worker_url and _is_mp_article(str(request.url)):
            wrapped = _wrap_worker_url(str(request.url), self._worker_url)
            request = httpx.Request(
                method=request.method,
                url=wrapped,
                headers=request.headers,
                stream=request.stream,
                extensions=request.extensions,
            )
        return await self._base.handle_async_request(request)

    async def aclose(self) -> None:
        await self._base.aclose()


class MPClient(AbstractAsyncContextManager):
    """Tiny async wrapper around httpx for the few endpoints we need."""

    @staticmethod
    def _build_async_client(**kwargs: Any) -> httpx.AsyncClient:
        try:
            return httpx.AsyncClient(**kwargs)
        except FileNotFoundError as exc:
            verify_paths = ssl.get_default_verify_paths()
            fallback_verify = certifi.where()
            logger.warning(
                'System CA bundle unavailable (cafile=%s, capath=%s); retrying with certifi bundle %s: %s',
                verify_paths.cafile,
                verify_paths.capath,
                fallback_verify,
                exc,
            )
            try:
                return httpx.AsyncClient(**{**kwargs, 'verify': fallback_verify})
            except FileNotFoundError as fallback_exc:
                raise RuntimeError(
                    'Failed to initialize HTTP TLS trust store '
                    f'(cafile={verify_paths.cafile}, capath={verify_paths.capath}, certifi={fallback_verify})'
                ) from fallback_exc

    def __init__(
        self,
        *,
        timeout: float = 30.0,
        proxy: str | None = None,
        article_worker: str | None = None,
        article_worker_proxy: str | None = None,
        article_max_connections: int | None = None,
        keepalive_connections: int | None = None,
    ) -> None:
        # No instance-wide proxy: every caller names the proxy for its own source
        # (see media_settings.resolve_proxy), because a WeChat fetch and a YouTube
        # fetch must not share one exit.
        settings = get_settings().http
        article_worker = settings.article_worker if article_worker is None else article_worker
        article_worker_proxy = settings.article_worker_proxy if article_worker_proxy is None else article_worker_proxy
        article_max_connections = (
            settings.article_max_connections if article_max_connections is None else article_max_connections
        )
        client_kwargs: dict[str, Any] = {
            'timeout': timeout,
            'headers': HEADERS,
            'follow_redirects': True,
        }
        if proxy:
            client_kwargs['proxy'] = proxy
        if keepalive_connections is not None:
            # The avatar guard pins each hop to a checked IP, so the pool keys by
            # that IP: a kept-alive channel could serve a different hostname
            # without a fresh handshake, skipping its certificate check.
            client_kwargs['limits'] = httpx.Limits(max_keepalive_connections=keepalive_connections)
        self.client = self._build_async_client(**client_kwargs)
        self.article_worker = article_worker.rstrip('/') if article_worker else None
        self.article_client: httpx.AsyncClient | None = None
        effective_article_proxy = article_worker_proxy or proxy
        if self.article_worker or article_worker_proxy:
            transport_kwargs: dict[str, Any] = {}
            if effective_article_proxy:
                transport_kwargs['proxy'] = effective_article_proxy
            if article_max_connections:
                limits = httpx.Limits(
                    max_connections=article_max_connections,
                    max_keepalive_connections=article_max_connections,
                )
                transport_kwargs['limits'] = limits
            transport = httpx.AsyncHTTPTransport(**transport_kwargs)
            wrapped_transport = WorkerURLTransport(transport, self.article_worker)
            self.article_client = self._build_async_client(
                timeout=timeout,
                headers=HEADERS,
                follow_redirects=True,
                transport=wrapped_transport,
            )

        logger.debug(
            'MPClient initialized: worker=%s, generic_proxy=%s, article_proxy=%s, max_conn=%s',
            self.article_worker or 'None',
            'enabled' if proxy else 'None',
            'enabled' if effective_article_proxy else 'None',
            article_max_connections or 'None',
        )

    @property
    def is_closed(self) -> bool:
        return self.client.is_closed or bool(self.article_client and self.article_client.is_closed)

    async def __aenter__(self) -> MPClient:
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:  # type: ignore[override]
        await self.aclose()

    async def aclose(self) -> None:
        await self.client.aclose()
        if self.article_client:
            await self.article_client.aclose()

    async def get(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        return await self.client.get(url, params=params, headers=headers)

    async def post(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        data: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> httpx.Response:
        return await self.client.post(url, params=params, headers=headers, data=data, json=json)

    async def fetch_article_html(self, url: str) -> str:
        client = self.article_client or self.client
        final_url = url
        if self.article_worker and _is_mp_article(url):
            final_url = _wrap_worker_url(url, self.article_worker)
            logger.debug('Wrapping article URL with worker: %s -> %s', url, final_url)
        logger.debug('Fetching article HTML: %s', final_url)
        try:
            resp = await client.get(final_url)
            resp.raise_for_status()
            text = resp.text
            if '该内容暂时无法查看' in text:
                raise ArticleContentUnavailableError(f'该内容暂时无法查看: {url}')
            logger.debug('Successfully fetched article: %s (size=%d bytes)', final_url, len(text))
            return text
        except Exception as exc:
            logger.error('Failed to fetch article %s: %s', final_url, exc)
            raise

    async def download_binary_with_type(self, url: str, *, referer: str | None = None) -> tuple[bytes, str | None]:
        headers = {}
        if referer:
            headers['Referer'] = referer
        resp = await self.client.get(url, headers=headers)
        resp.raise_for_status()
        content_type = resp.headers.get('content-type')
        return resp.content, content_type


# Parsing helpers -----------------------------------------------------------


def _is_mp_article(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except TypeError:
        return False
    return parsed.netloc.lower() == 'mp.weixin.qq.com' and '/s/' in parsed.path


def _wrap_worker_url(original: str, worker: str) -> str:
    encoded = quote(original, safe='')
    if '{url}' in worker:
        return worker.format(url=encoded)
    if worker.endswith(('?', '&', '=')):
        return f'{worker}{encoded}'
    separator = '&' if '?' in worker else '?'
    return f'{worker}{separator}url={encoded}'


__all__ = ['ArticleContentUnavailableError', 'MPClient']
