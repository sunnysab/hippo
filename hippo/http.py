"""HTTP helpers for interacting with WeChat endpoints."""

from __future__ import annotations

import json
import time
from http.cookies import SimpleCookie
from urllib.parse import parse_qs, quote, urlparse
from contextlib import AbstractAsyncContextManager
from typing import Any, Dict, Iterable, List, Optional

import httpx

from .config import (
    ARTICLE_WORKER_MAX_CONNECTIONS,
    ARTICLE_WORKER_PROXY,
    ARTICLE_WORKER_URL,
    DEFAULT_USER_AGENT,
)
from .logger import get_logger
from .models import ArticleRecord, LoginSession

logger = get_logger(__name__)

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


class SessionExpiredError(RuntimeError):
    pass


def _is_session_error_message(message: str) -> bool:
    lowered = message.lower()
    hints = ("invalid session", "invalid token", "session expired", "session timeout", "expired")
    return any(hint in lowered for hint in hints)


def _raise_for_base_resp(payload: Dict[str, Any], *, fallback: str) -> None:
    base_resp = payload.get("base_resp") or {}
    if base_resp.get("ret") == 0:
        return
    err_msg = str(base_resp.get("err_msg") or fallback)
    if _is_session_error_message(err_msg):
        raise SessionExpiredError(err_msg)
    raise RuntimeError(err_msg)


class MPClient(AbstractAsyncContextManager):
    """Tiny async wrapper around httpx for the few WeChat endpoints we need."""

    def __init__(
        self,
        *,
        timeout: float = 30.0,
        article_worker: Optional[str] = ARTICLE_WORKER_URL,
        article_worker_proxy: Optional[str] = ARTICLE_WORKER_PROXY,
        article_max_connections: Optional[int] = ARTICLE_WORKER_MAX_CONNECTIONS,
    ) -> None:
        self.client = httpx.AsyncClient(timeout=timeout, headers=HEADERS, follow_redirects=True)
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
            self.article_client = httpx.AsyncClient(
                timeout=timeout,
                headers=HEADERS,
                follow_redirects=True,
                transport=wrapped_transport,
            )
        
        logger.info(
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

    # ------------------------------------------------------------------
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
            logger.info("Successfully fetched article: %s (size=%d bytes)", final_url, len(resp.text))
            return resp.text
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

    # ------------------------------------------------------------------
    async def start_login_session(self, sid: str) -> str:
        payload = {
            "userlang": "zh_CN",
            "redirect_url": "",
            "login_type": 3,
            "sessionid": sid,
            "token": "",
            "lang": "zh_CN",
            "f": "json",
            "ajax": 1,
        }
        resp = await self.client.post(
            "https://mp.weixin.qq.com/cgi-bin/bizlogin",
            params={"action": "startlogin"},
            data=payload,
        )
        resp.raise_for_status()
        cookies = _parse_set_cookies(resp.headers.get_list("set-cookie"))
        uuid = cookies.get("uuid")
        if not uuid:
            raise RuntimeError("Failed to get login uuid cookie.")
        return f"uuid={uuid}"

    async def fetch_login_qrcode(self, cookie: str) -> bytes:
        resp = await self.client.get(
            "https://mp.weixin.qq.com/cgi-bin/scanloginqrcode",
            params={"action": "getqrcode", "random": int(time.time() * 1000)},
            headers={"Cookie": cookie},
        )
        resp.raise_for_status()
        return resp.content

    async def check_login_status(self, cookie: str) -> Dict[str, Any]:
        resp = await self.client.get(
            "https://mp.weixin.qq.com/cgi-bin/scanloginqrcode",
            params={"action": "ask", "token": "", "lang": "zh_CN", "f": "json", "ajax": 1},
            headers={"Cookie": cookie},
        )
        resp.raise_for_status()
        return resp.json()

    async def finalize_login(self, cookie: str) -> LoginSession:
        payload = {
            "userlang": "zh_CN",
            "redirect_url": "",
            "cookie_forbidden": 0,
            "cookie_cleaned": 0,
            "plugin_used": 0,
            "login_type": 3,
            "token": "",
            "lang": "zh_CN",
            "f": "json",
            "ajax": 1,
        }
        resp = await self.client.post(
            "https://mp.weixin.qq.com/cgi-bin/bizlogin",
            params={"action": "login"},
            data=payload,
            headers={"Cookie": cookie},
        )
        resp.raise_for_status()
        payload_json = resp.json()
        redirect_url = payload_json.get("redirect_url") or ""
        if not redirect_url:
            raise RuntimeError("Login failed: missing redirect_url")
        token = _extract_token(redirect_url)
        if not token:
            raise RuntimeError("Login failed: missing token")
        cookies = _parse_set_cookies(resp.headers.get_list("set-cookie"))
        return LoginSession(token=token, cookies=cookies)

    async def fetch_login_info(self, session: LoginSession) -> Dict[str, str]:
        resp = await self.client.get(
            "https://mp.weixin.qq.com/cgi-bin/home",
            params={"t": "home/index", "token": session.token, "lang": "zh_CN"},
            headers={"Cookie": _cookie_header(session.cookies)},
        )
        html = resp.text
        nickname = _match_value(html, r'wx\.cgiData\.nick_name\s*?=\s*?"(?P<value>[^"]+)"')
        avatar = _match_value(html, r'wx\.cgiData\.head_img\s*?=\s*?"(?P<value>[^"]+)"')
        return {"nickname": nickname or "", "avatar": avatar or ""}

    async def search_biz(
        self,
        session: LoginSession,
        *,
        keyword: str,
        begin: int = 0,
        count: int = 5,
    ) -> Dict[str, Any]:
        params = {
            "action": "search_biz",
            "begin": begin,
            "count": count,
            "query": keyword,
            "token": session.token,
            "lang": "zh_CN",
            "f": "json",
            "ajax": "1",
        }
        resp = await self.client.get(
            "https://mp.weixin.qq.com/cgi-bin/searchbiz",
            params=params,
            headers={"Cookie": _cookie_header(session.cookies)},
        )
        resp.raise_for_status()
        payload = resp.json()
        _raise_for_base_resp(payload, fallback="searchbiz failed")
        return payload

    async def fetch_appmsg_publish(
        self,
        session: LoginSession,
        *,
        fakeid: str,
        begin: int = 0,
        count: int = 5,
        keyword: str = "",
    ) -> Dict[str, Any]:
        is_searching = bool(keyword)
        params = {
            "sub": "search" if is_searching else "list",
            "search_field": "7" if is_searching else "null",
            "begin": begin,
            "count": count,
            "query": keyword,
            "fakeid": fakeid,
            "type": "101_1",
            "free_publish_type": 1,
            "sub_action": "list_ex",
            "token": session.token,
            "lang": "zh_CN",
            "f": "json",
            "ajax": 1,
        }
        resp = await self.client.get(
            "https://mp.weixin.qq.com/cgi-bin/appmsgpublish",
            params=params,
            headers={"Cookie": _cookie_header(session.cookies)},
        )
        resp.raise_for_status()
        payload = resp.json()
        _raise_for_base_resp(payload, fallback="appmsgpublish failed")
        return payload


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

def _parse_general_msg_list(raw: str) -> List[dict]:
    if not raw:
        return []
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Failed to parse general_msg_list") from exc
    return payload.get("list") or []


def _normalize_article_url(url: str | None) -> str:
    if not url:
        return ""
    value = url.replace("amp;", "").strip()
    if value.startswith("//"):
        value = "https:" + value
    if value.startswith("http://"):
        value = "https://" + value[len("http://") :]
    return value


def _parse_set_cookies(set_cookies: List[str]) -> Dict[str, str]:
    jar: Dict[str, str] = {}
    for item in set_cookies:
        cookie = SimpleCookie()
        cookie.load(item)
        for name, morsel in cookie.items():
            if morsel.value and morsel.value != "EXPIRED":
                jar[name] = morsel.value
    return jar


def _cookie_header(cookies: Dict[str, str]) -> str:
    return "; ".join(f"{k}={v}" for k, v in cookies.items())


def _extract_token(redirect_url: str) -> str:
    parsed = urlparse(redirect_url)
    qs = parse_qs(parsed.query)
    return (qs.get("token") or [""])[0]


def _match_value(html: str, pattern: str) -> str:
    import re

    match = re.search(pattern, html)
    if match and match.groupdict().get("value"):
        return match.group("value")
    return ""


def _message_to_articles(biz: str, message: dict) -> Iterable[ArticleRecord]:
    comm_info = message.get("comm_msg_info") or {}
    ext_info = message.get("app_msg_ext_info") or {}
    if not ext_info:
        return []

    records: List[ArticleRecord] = []
    primary = _build_article(biz, comm_info, ext_info, index=0)
    if primary:
        records.append(primary)
    if ext_info.get("is_multi"):
        for idx, item in enumerate(ext_info.get("multi_app_msg_item_list") or [], start=1):
            record = _build_article(biz, comm_info, item, index=idx)
            if record:
                records.append(record)
    return records


def _build_article(biz: str, comm: dict, item: dict, *, index: int) -> ArticleRecord | None:
    link = _normalize_article_url(item.get("content_url"))
    if not link:
        return None
    publish_at = comm.get("datetime")
    article_id = f"{comm.get('id')}-{index}"
    raw = {
        "comm_msg_info": comm,
        "app_msg_ext_info": item,
    }
    return ArticleRecord(
        biz=biz,
        article_id=article_id,
        title=item.get("title") or "(untitled)",
        author=item.get("author"),
        digest=item.get("digest"),
        cover=_normalize_article_url(item.get("cover")),
        link=link,
        source_url=_normalize_article_url(item.get("source_url")),
        publish_at=publish_at,
        raw=raw,
    )


def parse_appmsg_publish(fakeid: str, payload: Dict[str, Any]) -> List[ArticleRecord]:
    publish_page = {}
    raw_page = payload.get("publish_page") or "{}"
    try:
        publish_page = json.loads(raw_page)
    except json.JSONDecodeError:
        publish_page = {}
    publish_list = publish_page.get("publish_list") or []
    records: List[ArticleRecord] = []
    for item in publish_list:
        info_raw = item.get("publish_info")
        if not info_raw:
            continue
        try:
            info = json.loads(info_raw)
        except json.JSONDecodeError:
            continue
        for appmsg in info.get("appmsgex") or []:
            if appmsg.get("is_deleted"):
                continue
            link = _normalize_article_url(appmsg.get("link"))
            if not link:
                continue
            publish_at = appmsg.get("update_time") or appmsg.get("create_time")
            article_id = f"{appmsg.get('appmsgid')}-{appmsg.get('itemidx')}"
            records.append(
                ArticleRecord(
                    biz=fakeid,
                    article_id=article_id,
                    title=appmsg.get("title") or "(untitled)",
                    author=appmsg.get("author_name"),
                    digest=appmsg.get("digest"),
                    cover=_normalize_article_url(appmsg.get("cover") or appmsg.get("cover_img")),
                    link=link,
                    source_url=None,
                    publish_at=publish_at,
                    raw={"appmsgex": appmsg},
                )
            )
    return records


__all__ = ["MPClient", "parse_appmsg_publish"]
