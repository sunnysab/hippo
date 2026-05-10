"""HTTP helpers for interacting with WeChat endpoints."""

from __future__ import annotations

import ssl
from contextlib import AbstractAsyncContextManager
from typing import Any, Optional
from urllib.parse import quote, urlparse

import certifi
import httpx

from .config import (
    ARTICLE_WORKER_MAX_CONNECTIONS,
    ARTICLE_WORKER_PROXY,
    ARTICLE_WORKER_URL,
    DEFAULT_USER_AGENT,
)
from .logger import get_logger

logger = get_logger(__name__)


class ArticleContentUnavailableError(RuntimeError):
    pass

HEADERS = {
    "User-Agent": DEFAULT_USER_AGENT,
    "Referer": "https://mp.weixin.qq.com/",
    "Origin": "https://mp.weixin.qq.com",
}


class WorkerURLTransport(httpx.AsyncBaseTransport):
    def __init__(self, base: httpx.AsyncBaseTransport, worker_url: Optional[str]) -> None:
        self._base = base
        self._worker_url = worker_url.rstrip("/") if worker_url else None

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
        article_worker: Optional[str] = ARTICLE_WORKER_URL,
        article_worker_proxy: Optional[str] = ARTICLE_WORKER_PROXY,
        article_max_connections: Optional[int] = ARTICLE_WORKER_MAX_CONNECTIONS,
    ) -> None:
        self.client = self._build_async_client(
            timeout=timeout,
            headers=HEADERS,
            follow_redirects=True,
        )
        self.article_worker = article_worker.rstrip("/") if article_worker else None
        self.article_client: Optional[httpx.AsyncClient] = None
        if self.article_worker or article_worker_proxy:
            transport_kwargs: dict[str, Any] = {}
            if article_worker_proxy:
                transport_kwargs["proxy"] = article_worker_proxy
            if article_max_connections:
                limits = httpx.Limits(
                    max_connections=article_max_connections,
                    max_keepalive_connections=article_max_connections,
                )
                transport_kwargs["limits"] = limits
            transport = httpx.AsyncHTTPTransport(**transport_kwargs)
            wrapped_transport = WorkerURLTransport(transport, self.article_worker)
            self.article_client = self._build_async_client(
                timeout=timeout,
                headers=HEADERS,
                follow_redirects=True,
                transport=wrapped_transport,
            )

        logger.debug(
            "MPClient initialized: worker=%s, proxy=%s, max_conn=%s",
            self.article_worker or "None",
            article_worker_proxy or "None",
            article_max_connections or "None",
        )

    @property
    def is_closed(self) -> bool:
        if self.client.is_closed:
            return True
        if self.article_client and self.article_client.is_closed:
            return True
        return False

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
            logger.debug("Wrapping article URL with worker: %s -> %s", url, final_url)
        logger.debug("Fetching article HTML: %s", final_url)
        try:
            resp = await client.get(final_url)
            resp.raise_for_status()
            text = resp.text
            if "该内容暂时无法查看" in text:
                raise ArticleContentUnavailableError(f"该内容暂时无法查看: {url}")
            logger.debug("Successfully fetched article: %s (size=%d bytes)", final_url, len(text))
            return text
        except Exception as exc:
            logger.error("Failed to fetch article %s: %s", final_url, exc)
            raise

    async def download_binary(self, url: str, *, referer: str | None = None) -> bytes:
        headers = {}
        if referer:
            headers["Referer"] = referer
        logger.debug("Downloading binary: %s", url)
        try:
            resp = await self.client.get(url, headers=headers)
            resp.raise_for_status()
            logger.debug("Downloaded %d bytes from %s", len(resp.content), url)
            return resp.content
        except Exception as exc:
            logger.warning("Failed to download %s: %s", url, exc)
            raise

    async def download_binary_with_type(
        self, url: str, *, referer: str | None = None
    ) -> tuple[bytes, Optional[str]]:
        headers = {}
        if referer:
            headers["Referer"] = referer
        resp = await self.client.get(url, headers=headers)
        resp.raise_for_status()
        content_type = resp.headers.get("content-type")
        return resp.content, content_type


# Parsing helpers -----------------------------------------------------------


def _is_mp_article(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    return parsed.netloc.lower() == "mp.weixin.qq.com" and "/s/" in parsed.path


def _wrap_worker_url(original: str, worker: str) -> str:
    encoded = quote(original, safe="")
    if "{url}" in worker:
        return worker.format(url=encoded)
    if worker.endswith(("?", "&", "=")):
        return f"{worker}{encoded}"
    separator = "&" if "?" in worker else "?"
    return f"{worker}{separator}url={encoded}"


__all__ = ["MPClient", "ArticleContentUnavailableError"]
