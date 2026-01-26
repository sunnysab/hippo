"""Utilities for downloading article HTML and assets."""

from __future__ import annotations

import asyncio
import os
import re
from contextlib import AbstractAsyncContextManager
from typing import Iterable
from urllib.parse import parse_qs, urljoin, urlparse

from bs4 import BeautifulSoup
import httpx

from .normalize_html import normalize_html

from .env import load_env
from .http import MPClient
from .image_store import ArticleImageStore
from .logger import get_logger
from .models import AccountCredential, ArticleRecord, DownloadResult
from .storage import PostgresStorage
from .utils import slugify

load_env()

logger = get_logger(__name__)

_URL_PATTERN = re.compile(r"url\((?P<quote>[\"']?)(?P<url>[^\"')]+)(?P=quote)\)", re.IGNORECASE)
_ARTICLE_MAX_RETRIES = 5
_IMAGE_WORKERS = 2
_IMAGE_MAX_RETRIES = 3
_RETRY_BACKOFF_MAX = 10  # Maximum backoff time in seconds


def _extract_url_token(url: str) -> str | None:
    try:
        parsed = urlparse(url)
    except Exception:
        return None
    path = parsed.path or ""
    if "/s/" in path:
        token = path.split("/s/", 1)[1]
        if token:
            return token.split("?", 1)[0]
    return None


def _is_http_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    return parsed.scheme in ("http", "https")


def _resolve_asset_url(url: str, *, base: str) -> str | None:
    if not url:
        return None
    lowered = url.strip().lower()
    if lowered.startswith(("data:", "javascript:", "about:", "file:")):
        logger.debug("Skipping invalid URL scheme: %s", url[:100])
        return None
    
    # Handle Sogou proxy URLs: extract the actual WeChat URL from url= parameter
    # Match any sogou.com domain, not just specific subdomains
    if "sogou.com" in lowered and ("url=" in lowered or "url%3d" in lowered):
        parsed = urlparse(url)
        if parsed.query:
            params = parse_qs(parsed.query)
            if "url" in params and params["url"]:
                actual_url = params["url"][0]
                # Accept any URL that looks like a WeChat image
                if any(domain in actual_url.lower() for domain in ["mmbiz.qpic.cn", "wx.qlogo.cn", "mmbiz.qlogo.cn", "weixin.qq.com"]):
                    logger.debug("Unwrapped Sogou proxy URL: %s -> %s", url[:100], actual_url[:100])
                    url = actual_url
                else:
                    # Even if not WeChat, try to use the extracted URL
                    logger.debug("Extracted URL from Sogou proxy (non-WeChat): %s -> %s", url[:100], actual_url[:100])
                    url = actual_url
    
    if url.startswith("//"):
        resolved = f"https:{url}"
    else:
        parsed = urlparse(url)
        if parsed.scheme:
            resolved = url
        else:
            resolved = urljoin(base, url)
    parsed_resolved = urlparse(resolved)
    if parsed_resolved.scheme not in ("http", "https"):
        logger.debug("Skipping non-HTTP(S) URL: %s", resolved[:100])
        return None
    if not parsed_resolved.path or parsed_resolved.path == "/":
        logger.debug("Skipping URL without valid path: %s", resolved[:100])
        return None
    return resolved


def _parse_markdown_blocks(markdown: str) -> tuple[str | None, str | None, list[dict], str]:
    lines = markdown.splitlines()
    title: str | None = None
    cover_local: str | None = None
    blocks: list[dict] = []
    body_lines: list[str] = []
    paragraph: list[str] = []
    image_pattern = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")
    heading_pattern = re.compile(r"^(#{1,6})\s+(.*)$")
    link_pattern = re.compile(r"^[*-]\s+(https?://\S+)$")

    def flush_paragraph() -> None:
        nonlocal paragraph
        if paragraph:
            text = " ".join(paragraph).strip()
            if text:
                blocks.append({"type": "paragraph", "text": text})
            paragraph = []

    for line in lines:
        stripped = line.strip()
        if not stripped:
            flush_paragraph()
            if body_lines and body_lines[-1] != "":
                body_lines.append("")
            continue
        image_match = image_pattern.fullmatch(stripped)
        if image_match:
            flush_paragraph()
            alt, url = image_match.groups()
            if cover_local is None and not blocks and not body_lines and title is None:
                cover_local = url
            else:
                blocks.append({"type": "image", "alt": alt, "local_path": url})
                body_lines.append(f"![{alt}]({url})")
            continue
        heading_match = heading_pattern.match(stripped)
        if heading_match:
            flush_paragraph()
            level = len(heading_match.group(1))
            text = heading_match.group(2).strip()
            if title is None and level == 1:
                title = text
            else:
                blocks.append({"type": "heading", "level": level, "text": text})
                body_lines.append(stripped)
            continue
        link_match = link_pattern.match(stripped)
        if link_match:
            flush_paragraph()
            url = link_match.group(1)
            blocks.append({"type": "link", "text": url, "href": url})
            body_lines.append(stripped)
            continue
        paragraph.append(stripped)
        body_lines.append(stripped)
    flush_paragraph()
    body_markdown = "\n".join(body_lines).strip()
    return title, cover_local, blocks, body_markdown


class ArticleDownloader(AbstractAsyncContextManager):
    def __init__(
        self,
        *,
        client: MPClient | None = None,
        storage: PostgresStorage | None = None,
        image_store: ArticleImageStore | None = None,
        article_worker: str | None = None,
        article_worker_proxy: str | None = None,
        article_max_connections: int | None = None,
        image_workers: int | None = None,
        enable_image_worker: bool = True,
    ) -> None:
        self._managed_client = client is None
        self.client = client or MPClient(
            article_worker=article_worker,
            article_worker_proxy=article_worker_proxy,
            article_max_connections=article_max_connections,
        )
        self.storage = storage
        self._image_store = image_store
        self._enable_image_worker = enable_image_worker
        self._article_workers = (
            article_max_connections if article_max_connections and article_max_connections > 0 else 1
        )
        self._image_workers = image_workers if image_workers and image_workers > 0 else _IMAGE_WORKERS
        self._image_semaphore = asyncio.Semaphore(self._image_workers)
        self._image_total = 0
        self._image_done = 0
        self._image_lock = asyncio.Lock()
        self._image_abort = False
        self._image_max_retries = _IMAGE_MAX_RETRIES
        
        logger.info(
            "ArticleDownloader initialized: article_workers=%d, image_workers=%d",
            self._article_workers,
            self._image_workers,
        )

    async def __aenter__(self) -> ArticleDownloader:
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:  # type: ignore[override]
        if exc_type is KeyboardInterrupt:
            await self.abort_image_queue()
        await self.aclose()

    async def aclose(self) -> None:
        if self._managed_client:
            await self.client.aclose()

    async def wait_for_images(self) -> None:
        return None

    async def wait_for_images_with_progress(self, *, label: str = "下载图片") -> None:
        total, done = await self._image_stats()
        if total == 0 or done >= total:
            return
        try:
            from tqdm import tqdm
        except Exception:
            return
        bar = tqdm(
            total=total,
            desc=label,
            unit="张",
            dynamic_ncols=True,
            leave=True,
        )
        try:
            while True:
                total, done = await self._image_stats()
                if total > bar.total:
                    bar.total = total
                bar.n = done
                bar.refresh()
                if done >= total:
                    break
                await asyncio.sleep(0.2)
        finally:
            bar.close()

    async def abort_image_queue(self) -> None:
        self._image_abort = True

    # ------------------------------------------------------------------
    async def download_many(
        self,
        articles: Iterable[ArticleRecord],
        *,
        with_images: bool = True,
        record_images_only: bool = False,
        progress: object | None = None,
        skip_if_downloaded: bool = True,
    ) -> tuple[list[DownloadResult], int, int]:
        """Download multiple articles.
        
        Returns:
            tuple of (results, skipped_count, failed_count)
        """
        results: list[DownloadResult] = []
        skipped = 0
        failed = 0
        pending: list[ArticleRecord] = []
        articles_list = list(articles)
        content_ids: set[str] | None = None
        if self.storage and os.environ.get("HIPPO_PG_DSN"):
            get_content_ids = getattr(self.storage, "get_article_content_ids", None)
            if callable(get_content_ids):
                try:
                    article_ids = [article.article_id for article in articles_list]
                    if article_ids:
                        content_ids = set(get_content_ids(articles_list[0].biz, article_ids))
                except Exception:
                    content_ids = None
        for article in articles_list:
            if skip_if_downloaded:
                if content_ids is not None:
                    if article.article_id in content_ids:
                        skipped += 1
                        if progress is not None:
                            progress.update(1)
                        continue
                else:
                    if self._is_downloaded(article):
                        skipped += 1
                        if progress is not None:
                            progress.update(1)
                        continue
            pending.append(article)

        if not pending:
            return results, skipped, failed

        async def download_one(target: ArticleRecord) -> DownloadResult:
            return await self._download_with_retry(
                target,
                with_images=with_images,
                record_images_only=record_images_only,
            )

        if self._article_workers <= 1:
            try:
                for article in pending:
                    try:
                        result = await download_one(article)
                        results.append(result)
                    except Exception as exc:
                        failed += 1
                        self._log_download_error(
                            stage="article_download",
                            article=article,
                            error=str(exc),
                        )
                        if progress is not None:
                            progress.write(
                                f"下载失败：{article.title} ({article.article_id}) {exc}"
                            )
                            progress.update(1)
                        continue
                    if progress is not None:
                        progress.update(1)
            except KeyboardInterrupt:
                await self.abort_image_queue()
                raise
            return results, skipped, failed

        sem = asyncio.Semaphore(self._article_workers)

        async def run(article: ArticleRecord) -> DownloadResult:
            async with sem:
                return await download_one(article)

        # Map tasks to articles
        active_tasks = {asyncio.create_task(run(article)): article for article in pending}
        
        try:
            while active_tasks:
                done, _ = await asyncio.wait(
                    active_tasks.keys(),
                    return_when=asyncio.FIRST_COMPLETED
                )
                
                for task in done:
                    article = active_tasks.pop(task)
                    try:
                        result = await task
                    except Exception as exc:
                        failed += 1
                        self._log_download_error(
                            stage="article_download",
                            article=article,
                            error=str(exc),
                        )
                        if progress is not None:
                            progress.write(
                                f"下载失败：{article.title} ({article.article_id}) {exc}"
                            )
                    else:
                        results.append(result)
                    
                    if progress is not None:
                        progress.update(1)

        except Exception:
            await self.abort_image_queue()
            # Cancel all remaining tasks
            for task in active_tasks:
                task.cancel()
            # Wait for cancellations to propagate
            if active_tasks:
                await asyncio.gather(*active_tasks.keys(), return_exceptions=True)
            raise
        return results, skipped, failed

    async def download_from_url(
        self,
        url: str,
        *,
        with_images: bool = True,
        record_images_only: bool = False,
        title: str | None = None,
    ) -> DownloadResult:
        raw_html = await self._fetch_with_retry(url)
        inferred_title = title or _extract_title(raw_html) or "WeChat Article"
        token = _extract_url_token(url)
        stub = ArticleRecord(
            biz="adhoc",
            article_id=token or slugify(inferred_title),
            title=inferred_title,
            author=None,
            digest=None,
            cover=None,
            link=url,
            source_url=url,
            publish_at=None,
            raw={"source": "adhoc"},
        )
        self._ensure_adhoc_account(stub.biz)
        return await self._persist_article(
            stub,
            raw_html=raw_html,
            with_images=with_images,
            record_images_only=record_images_only,
        )

    def _ensure_adhoc_account(self, biz: str) -> None:
        if biz != 'adhoc' or not self.storage:
            return
        account_repo = getattr(self.storage, 'accounts', None)
        get_account = getattr(account_repo, 'get_account', None) if account_repo else None
        upsert_account = getattr(account_repo, 'upsert_account', None) if account_repo else None
        if not callable(upsert_account):
            upsert_account = getattr(self.storage, 'upsert_account', None)
            get_account = getattr(self.storage, 'get_account', None)
        if not callable(upsert_account):
            return
        if callable(get_account):
            try:
                get_account(biz, fallback_to_default=False)
                return
            except LookupError:
                pass
        credential = AccountCredential(
            biz=biz,
            nickname=biz,
            alias=None,
            round_head_img=None,
        )
        transaction = getattr(self.storage, 'transaction', None)
        if callable(transaction):
            with transaction():
                upsert_account(credential)
        else:
            upsert_account(credential)

    async def _fetch_with_retry(self, url: str) -> str:
        last_exc: Exception | None = None
        for attempt in range(_ARTICLE_MAX_RETRIES):
            try:
                logger.debug("Fetching article (attempt %d/%d): %s", attempt + 1, _ARTICLE_MAX_RETRIES, url)
                return await self.client.fetch_article_html(url)
            except Exception as exc:
                last_exc = exc
                # Calculate backoff time with jitter
                backoff = min(2 ** attempt, _RETRY_BACKOFF_MAX)
                jitter = backoff * 0.1 * (0.5 + 0.5 * (attempt % 2))
                wait_time = backoff + jitter
                
                logger.warning(
                    "Fetch failed (attempt %d/%d) for %s: %s - retrying in %.1fs",
                    attempt + 1,
                    _ARTICLE_MAX_RETRIES,
                    url,
                    exc,
                    wait_time,
                )
                
                if attempt < _ARTICLE_MAX_RETRIES - 1:
                    await asyncio.sleep(wait_time)
        
        logger.error("Failed to fetch article after %d retries: %s", _ARTICLE_MAX_RETRIES, url)
        raise RuntimeError(f"下载失败：{last_exc}") from last_exc

    async def _download_with_retry(
        self,
        article: ArticleRecord,
        *,
        with_images: bool,
        record_images_only: bool,
    ) -> DownloadResult:
        last_exc: Exception | None = None
        for attempt in range(_ARTICLE_MAX_RETRIES):
            try:
                logger.debug(
                    "Downloading article (attempt %d/%d): %s - %s",
                    attempt + 1,
                    _ARTICLE_MAX_RETRIES,
                    article.article_id,
                    article.title,
                )
                html = await self.client.fetch_article_html(article.link)
                result = await self._persist_article(
                    article,
                    raw_html=html,
                    with_images=with_images,
                    record_images_only=record_images_only,
                )
                logger.info("Successfully downloaded article: %s - %s", article.article_id, article.title)
                return result
            except Exception as exc:
                last_exc = exc
                # Calculate backoff time with jitter
                backoff = min(2 ** attempt, _RETRY_BACKOFF_MAX)
                # Add small jitter to avoid thundering herd
                jitter = backoff * 0.1 * (0.5 + 0.5 * (attempt % 2))
                wait_time = backoff + jitter
                
                logger.warning(
                    "Download failed (attempt %d/%d) for %s: %s - retrying in %.1fs",
                    attempt + 1,
                    _ARTICLE_MAX_RETRIES,
                    article.article_id,
                    exc,
                    wait_time,
                )
                
                if attempt < _ARTICLE_MAX_RETRIES - 1:
                    await asyncio.sleep(wait_time)
        
        logger.error("Failed to download article after %d retries: %s - %s", _ARTICLE_MAX_RETRIES, article.article_id, article.title)
        raise RuntimeError(f"下载失败：{last_exc}") from last_exc

    # ------------------------------------------------------------------
    async def _persist_article(
        self,
        article: ArticleRecord,
        *,
        raw_html: str,
        with_images: bool,
        record_images_only: bool,
    ) -> DownloadResult:

        clean_html = normalize_html(raw_html, fmt="html")
        asset_count = 0
        url_map: dict[str, str] = {}
        referer = article.link or "https://mp.weixin.qq.com/"
        if with_images or record_images_only:
            asset_count, url_map = self._collect_image_urls(
                clean_html, referer=referer
            )

        try:
            markdown_content = normalize_html(
                raw_html, fmt="markdown", markdown_image_map=url_map
            )
        except RecursionError as exc:
            logger.warning(
                "Markdown conversion hit recursion limit: %s - %s",
                article.article_id,
                exc,
            )
            try:
                markdown_content = normalize_html(raw_html, fmt="text")
            except Exception as text_exc:
                logger.warning(
                    "Markdown fallback failed: %s - %s",
                    article.article_id,
                    text_exc,
                )
                markdown_content = ""
        pg_error: Exception | None = None
        try:
            self._store_article_pg(
                article=article,
                clean_html=clean_html,
                markdown_content=markdown_content,
                url_map=url_map,
            )
        except Exception as exc:
            pg_error = exc

        if pg_error:
            raise pg_error

        if with_images and url_map:
            await self._enqueue_image_downloads(
                article, url_map, referer=referer
            )

        return DownloadResult(article=article, asset_count=asset_count)

    def _store_article_pg(
        self,
        *,
        article: ArticleRecord,
        clean_html: str,
        markdown_content: str,
        url_map: dict[str, str],
    ) -> None:
        if not self.storage:
            return
        if not hasattr(self.storage, "save_article_content"):
            return
        title, cover_local, blocks, body_markdown = _parse_markdown_blocks(markdown_content)
        base_url = article.link or "https://mp.weixin.qq.com/"
        resolved_map = {raw: resolved for raw, resolved in url_map.items() if resolved}

        def resolve_url(value: str | None) -> str | None:
            if not value:
                return None
            if value in resolved_map:
                return resolved_map[value]
            return _resolve_asset_url(value, base=base_url) or value

        content_markdown = body_markdown
        for raw, resolved in resolved_map.items():
            content_markdown = content_markdown.replace(f"]({raw})", f"]({resolved})")

        cover_url = resolve_url(cover_local) or resolve_url(article.cover)
        images: list[dict] = []
        image_positions: list[tuple[str, str]] = []
        if cover_local:
            image_positions.append(("cover", cover_local))
        for block in blocks:
            if block.get("type") == "image":
                image_positions.append(("inline", block.get("local_path")))
        position = 0
        for kind, local_path in image_positions:
            if not local_path:
                continue
            orig_url = resolve_url(str(local_path))
            if not orig_url:
                continue
            images.append(
                {
                    "orig_url": orig_url,
                    "kind": kind,
                    "position": position,
                    "content_type": None,
                    "data": None,
                }
            )
            position += 1

        url_token = _extract_url_token(article.link)
        blocks_with_urls: list[dict] = []
        for block in blocks:
            if block.get("type") == "image":
                local_path = block.get("local_path")
                orig_url = resolve_url(str(local_path)) if local_path else None
                updated = dict(block)
                updated.pop("local_path", None)
                updated["orig_url"] = orig_url
                blocks_with_urls.append(updated)
            else:
                blocks_with_urls.append(block)

        if hasattr(self.storage, 'transaction'):
            with self.storage.transaction():
                self.storage.articles.save_article_content(
                    article,
                    url_token=url_token,
                    title=title or article.title,
                    clean_html=clean_html,
                    content_markdown=content_markdown,
                    content_blocks=blocks_with_urls,
                    cover_url=cover_url,
                    images=images,
                )
        else:
            self.storage.articles.save_article_content(
                article,
                url_token=url_token,
                title=title or article.title,
                clean_html=clean_html,
                content_markdown=content_markdown,
                content_blocks=blocks_with_urls,
                cover_url=cover_url,
                images=images,
            )

    def _is_downloaded(self, article: ArticleRecord) -> bool:
        if self.storage and os.environ.get("HIPPO_PG_DSN"):
            repo = getattr(self.storage, 'articles', None)
            has_content = getattr(repo, 'has_article_content', None) if repo else None
            if callable(has_content):
                try:
                    return bool(has_content(article.biz, article.article_id))
                except Exception:
                    return False
        return False

    def _collect_image_urls(
        self, html: str, *, referer: str
    ) -> tuple[int, dict[str, str]]:
        soup = BeautifulSoup(html, "html.parser")
        count = 0
        url_map: dict[str, str] = {}

        def add_url(url: str) -> None:
            nonlocal count
            resolved = _resolve_asset_url(url, base=referer)
            if not resolved:
                return
            if url in url_map:
                return
            count += 1
            url_map[url] = resolved

        for img in soup.find_all("img"):
            src = img.get("src") or img.get("data-src")
            if not src or src.startswith("data:"):
                continue
            add_url(src)

        def extract_from_style(text: str) -> None:
            for match in _URL_PATTERN.finditer(text):
                candidate = match.group("url").strip()
                if not candidate or candidate.startswith("data:"):
                    continue
                add_url(candidate)

        for node in soup.find_all(style=True):
            style = node.get("style") or ""
            if "url(" not in style:
                continue
            extract_from_style(style)

        for style_tag in soup.find_all("style"):
            content = style_tag.string
            if not content or "url(" not in content:
                continue
            extract_from_style(content)

        return count, url_map

    async def _image_stats(self) -> tuple[int, int]:
        async with self._image_lock:
            return self._image_total, self._image_done

    async def _mark_image_total(self, count: int) -> None:
        if count <= 0:
            return
        async with self._image_lock:
            self._image_total += count

    async def _mark_image_done(self) -> None:
        async with self._image_lock:
            self._image_done += 1

    def _record_image_failure(self, *, article: ArticleRecord, orig_url: str, reason: str) -> None:
        if self._image_store and orig_url:
            self._image_store.mark_failed(
                biz=article.biz,
                article_id=article.article_id,
                orig_url=orig_url,
                reason=reason,
            )
            return
        if self.storage and orig_url:
            self.storage.images.mark_article_image_failed(
                article.biz,
                article.article_id,
                orig_url,
                reason,
            )

    async def _download_one_image(
        self,
        *,
        article: ArticleRecord,
        resolved_url: str,
        orig_url: str,
        referer: str,
    ) -> None:
        if self._image_abort:
            self._log_download_error(
                stage="asset_download",
                article=article,
                error="aborted",
                asset_url=orig_url,
                resolved_url=resolved_url,
                referer=referer,
            )
            await self._mark_image_done()
            return
        if not _is_http_url(str(resolved_url)):
            reason = f"Invalid URL scheme (non-http): {resolved_url}"
            self._log_download_error(
                stage="asset_download",
                article=article,
                error=reason,
                asset_url=orig_url,
                resolved_url=resolved_url,
                referer=referer,
            )
            self._record_image_failure(article=article, orig_url=orig_url, reason=reason)
            await self._mark_image_done()
            return

        async with self._image_semaphore:
            for attempt in range(1, self._image_max_retries + 1):
                if self._image_abort:
                    self._log_download_error(
                        stage="asset_download",
                        article=article,
                        error="aborted",
                        asset_url=orig_url,
                        resolved_url=resolved_url,
                        referer=referer,
                    )
                    await self._mark_image_done()
                    return
                try:
                    data, content_type = await self.client.download_binary_with_type(
                        resolved_url, referer=referer
                    )
                    if self._image_store and orig_url:
                        self._image_store.store(
                            biz=article.biz,
                            article_id=article.article_id,
                            orig_url=orig_url,
                            content_type=content_type,
                            data=data,
                        )
                    elif orig_url:
                        reason = 'Image store not configured'
                        self._record_image_failure(article=article, orig_url=orig_url, reason=reason)
                        await self._mark_image_done()
                        return
                    await self._mark_image_done()
                    return
                except Exception as exc:
                    is_http_400_or_404 = False
                    if isinstance(exc, httpx.HTTPStatusError) and exc.response is not None:
                        status = exc.response.status_code
                        is_http_400_or_404 = status in (400, 404)
                    if is_http_400_or_404 or attempt >= self._image_max_retries:
                        self._log_download_error(
                            stage="asset_download",
                            article=article,
                            error=str(exc),
                            asset_url=orig_url,
                            resolved_url=resolved_url,
                            referer=referer,
                        )
                        self._record_image_failure(article=article, orig_url=orig_url, reason=str(exc))
                        await self._mark_image_done()
                        return
                    await asyncio.sleep(min(2 ** attempt, _RETRY_BACKOFF_MAX))

    async def _download_images_for_article(
        self,
        article: ArticleRecord,
        urls: list[str],
        *,
        referer: str,
    ) -> None:
        if not urls or not self._enable_image_worker:
            return
        await self._mark_image_total(len(urls))
        async with asyncio.TaskGroup() as tg:
            for resolved in urls:
                tg.create_task(
                    self._download_one_image(
                        article=article,
                        resolved_url=resolved,
                        orig_url=resolved,
                        referer=referer,
                    )
                )

    async def _enqueue_image_downloads(
        self, article: ArticleRecord, url_map: dict[str, str], *, referer: str
    ) -> None:
        if not url_map:
            return
        seen: set[str] = set()
        urls: list[str] = []
        for resolved in url_map.values():
            if not resolved or resolved in seen:
                continue
            seen.add(resolved)
            urls.append(resolved)
        await self._download_images_for_article(article, urls, referer=referer)

    def _log_download_error(
        self,
        *,
        stage: str,
        article: ArticleRecord,
        error: str,
        asset_url: str | None = None,
        resolved_url: str | None = None,
        referer: str | None = None,
    ) -> None:
        payload = {
            "stage": stage,
            "error": error,
            "article": {
                "biz": article.biz,
                "article_id": article.article_id,
                "title": article.title,
                "link": article.link,
            },
        }
        if asset_url:
            payload["asset_url"] = asset_url
        if resolved_url:
            payload["resolved_url"] = resolved_url
        if referer:
            payload["referer"] = referer
        logger.warning("Download error: %s", payload)


# Helper functions ---------------------------------------------------------
def _extract_title(raw_html: str) -> str | None:
    soup = BeautifulSoup(raw_html, "html.parser")
    if soup.title and soup.title.string:
        return soup.title.string.strip()
    h1 = soup.find("h1")
    if h1 and h1.get_text(strip=True):
        return h1.get_text(strip=True)
    return None


__all__ = ["ArticleDownloader"]
