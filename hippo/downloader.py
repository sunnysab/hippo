"""Utilities for downloading article HTML and assets."""

from __future__ import annotations

import concurrent.futures
import json
import os
import mimetypes
import queue
import re
import threading
import time
import errno
from queue import Empty
from typing import Callable, Dict, Tuple
from datetime import datetime, timezone
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Iterable, List, Optional
from urllib.parse import urlparse, urljoin, parse_qs

from bs4 import BeautifulSoup
import httpx

try:
    # Running from repo root: python/ is a package.
    from ..normalize_html import normalize_html
except ImportError:  # pragma: no cover - fallback for running inside python/
    from normalize_html import normalize_html

from .config import DOWNLOAD_ROOT, HOME_DIR
from .http import MPClient
from .logger import get_logger
from .models import ArticleRecord, DownloadResult
from .storage import StorageLike
from .utils import ensure_directory, slugify, timestamp_to_datestr

logger = get_logger(__name__)

_FORMAT_EXTENSIONS = {
    "html": "index.html",
    "markdown": "article.md",
    "text": "article.txt",
}
_URL_PATTERN = re.compile(r"url\((?P<quote>[\"']?)(?P<url>[^\"')]+)(?P=quote)\)", re.IGNORECASE)
_ARTICLE_MAX_RETRIES = 5
_IMAGE_WORKERS = 2
_IMAGE_MAX_RETRIES = 3
_ERROR_LOG_NAME = "download_errors.jsonl"
_SUFFIX_PATTERN = re.compile(r"^(?P<base>.+)-(?P<index>\d+)$")
_RETRY_BACKOFF_MAX = 10  # Maximum backoff time in seconds


def _extract_url_token(url: str) -> Optional[str]:
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


def _resolve_asset_url(url: str, *, base: str) -> Optional[str]:
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


def _parse_markdown_blocks(markdown: str) -> Tuple[Optional[str], Optional[str], list[dict], str]:
    lines = markdown.splitlines()
    title: Optional[str] = None
    cover_local: Optional[str] = None
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


def _safe_title_segment(title: Optional[str]) -> str:
    if not title:
        return "article"
    sanitized = title.strip()
    if not sanitized:
        return "article"
    sanitized = sanitized.replace(os.sep, "-")
    if os.altsep:
        sanitized = sanitized.replace(os.altsep, "-")
    sanitized = sanitized.replace(":", "-")
    sanitized = re.sub(r"\s+", " ", sanitized)
    sanitized = sanitized.strip(" .")
    sanitized = _truncate_utf8(sanitized, 120)
    return sanitized or "article"


def _truncate_utf8(value: str, max_bytes: int) -> str:
    raw = value.encode("utf-8")
    if len(raw) <= max_bytes:
        return value
    chunk = raw[:max_bytes]
    while chunk:
        try:
            return chunk.decode("utf-8").rstrip(" .-")
        except UnicodeDecodeError:
            chunk = chunk[:-1]
    return ""


def _article_base_segment(article: ArticleRecord) -> str:
    title_segment = _safe_title_segment(article.title) or article.article_id or "article"
    base_segment = f"{timestamp_to_datestr(article.publish_at)}-{title_segment}"
    trimmed = _truncate_utf8(base_segment, 180)
    return trimmed or "article"


class ImageDownloadWorker:
    def __init__(
        self,
        *,
        log_error: Callable[..., None],
        pg_dsn: Optional[str],
        workers: int = _IMAGE_WORKERS,
        max_retries: int = _IMAGE_MAX_RETRIES,
    ) -> None:
        self._log_error = log_error
        self._pg_dsn = pg_dsn
        self._max_retries = max_retries
        self._queue: "queue.Queue[Optional[dict]]" = queue.Queue()
        self._stop = threading.Event()
        self._closed = False
        self._total = 0
        self._done = 0
        self._lock = threading.Lock()
        self._threads = [
            threading.Thread(target=self._run, daemon=True)
            for _ in range(max(1, workers))
        ]
        for thread in self._threads:
            thread.start()

    def enqueue(self, task: dict) -> None:
        if self._closed:
            return
        if not task.get("_counted"):
            task["_counted"] = True
            with self._lock:
                self._total += 1
        self._queue.put(task)

    def wait(self) -> None:
        self._queue.join()

    def stats(self) -> tuple[int, int]:
        with self._lock:
            return self._total, self._done

    def mark_pending_as_failed(self) -> None:
        if self._closed:
            return
        self._stop.set()
        while True:
            try:
                task = self._queue.get_nowait()
            except Empty:
                break
            try:
                if task is None:
                    continue
                article = task.get("article")
                resolved_url = task.get("resolved_url")
                orig_url = task.get("orig_url")
                referer = task.get("referer")
                target_dir = task.get("target_dir")
                local_path = task.get("local_path")
                if article and resolved_url and target_dir and local_path:
                    self._log_error(
                        stage="asset_download",
                        article=article,
                        error="aborted",
                        asset_url=orig_url or resolved_url,
                        resolved_url=resolved_url,
                        referer=referer,
                        target_dir=target_dir,
                        local_path=local_path,
                    )
                with self._lock:
                    self._done += 1
            finally:
                self._queue.task_done()

    def close(self) -> None:
        if self._closed:
            return
        try:
            self._queue.join()
        except KeyboardInterrupt:
            self._stop.set()
        finally:
            self._closed = True
            for _ in self._threads:
                self._queue.put(None)
            for thread in self._threads:
                thread.join(timeout=2)

    def _run(self) -> None:
        storage = None
        if self._pg_dsn:
            from .storage import PostgresStorage

            storage = PostgresStorage(self._pg_dsn)
        with MPClient(timeout=15.0) as client:
            while True:
                task = self._queue.get()
                if task is None:
                    self._queue.task_done()
                    break
                if self._stop.is_set():
                    self._queue.task_done()
                    continue
                article = task["article"]
                resolved_url = task["resolved_url"]
                orig_url = task.get("orig_url")
                referer = task.get("referer")
                target_dir = task["target_dir"]
                local_path = task["local_path"]
                attempt = int(task.get("attempt", 1))
                if not _is_http_url(str(resolved_url)):
                    reason = f"Invalid URL scheme (non-http): {resolved_url}"
                    self._log_error(
                        stage="asset_download",
                        article=article,
                        error=reason,
                        asset_url=orig_url or resolved_url,
                        resolved_url=resolved_url,
                        referer=referer,
                        target_dir=target_dir,
                        local_path=local_path,
                    )
                    if storage and orig_url:
                        storage.mark_article_image_failed(
                            article.biz,
                            article.article_id,
                            str(orig_url),
                            reason,
                        )
                    with self._lock:
                        self._done += 1
                    self._queue.task_done()
                    continue
                try:
                    data, content_type = client.download_binary_with_type(
                        resolved_url, referer=referer
                    )
                    file_path = target_dir / local_path
                    ensure_directory(file_path.parent)
                    file_path.write_bytes(data)
                    if storage and orig_url:
                        storage.update_article_image_data(
                            article.biz,
                            article.article_id,
                            str(orig_url),
                            content_type,
                            data,
                        )
                    with self._lock:
                        self._done += 1
                except Exception as exc:
                    is_http_400_or_404 = False
                    if isinstance(exc, httpx.HTTPStatusError) and exc.response is not None:
                        status = exc.response.status_code
                        is_http_400_or_404 = status in (400, 404)
                    if (
                        not is_http_400_or_404
                        and attempt < self._max_retries
                        and not self._stop.is_set()
                    ):
                        task["attempt"] = attempt + 1
                        self._queue.put(task)
                    else:
                        self._log_error(
                            stage="asset_download",
                            article=article,
                            error=str(exc),
                            asset_url=orig_url or resolved_url,
                            resolved_url=resolved_url,
                            referer=referer,
                            target_dir=target_dir,
                            local_path=local_path,
                        )
                        if storage and orig_url:
                            storage.mark_article_image_failed(
                                article.biz,
                                article.article_id,
                                str(orig_url),
                                str(exc),
                            )
                        with self._lock:
                            self._done += 1
                finally:
                    self._queue.task_done()
        if storage:
            storage.close()


class ArticleDownloader(AbstractContextManager):
    def __init__(
        self,
        *,
        client: Optional[MPClient] = None,
        output_dir: Optional[Path] = None,
        storage: Optional[StorageLike] = None,
        article_worker: Optional[str] = None,
        article_worker_proxy: Optional[str] = None,
        article_max_connections: Optional[int] = None,
        image_workers: Optional[int] = None,
        enable_image_worker: bool = True,
    ) -> None:
        self._managed_client = client is None
        self.client = client or MPClient(
            article_worker=article_worker,
            article_worker_proxy=article_worker_proxy,
            article_max_connections=article_max_connections,
        )
        self.output_dir = ensure_directory(output_dir or DOWNLOAD_ROOT)
        self.storage = storage
        self._pg_dsn = os.environ.get("HIPPO_PG_DSN")
        self._image_worker: Optional[ImageDownloadWorker] = None
        self._enable_image_worker = enable_image_worker
        self._article_workers = article_max_connections if article_max_connections and article_max_connections > 0 else 1
        self._image_workers = image_workers if image_workers and image_workers > 0 else _IMAGE_WORKERS
        
        logger.info(
            "ArticleDownloader initialized: output=%s, article_workers=%d, image_workers=%d",
            self.output_dir,
            self._article_workers,
            self._image_workers,
        )

    def __enter__(self) -> "ArticleDownloader":
        if self._enable_image_worker:
            self._retry_failed_images()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:  # type: ignore[override]
        if exc_type is KeyboardInterrupt:
            self.abort_image_queue()
        self.close()

    def close(self) -> None:
        if self._image_worker:
            self._image_worker.close()
        if self._managed_client:
            self.client.close()

    def _ensure_image_worker(self) -> ImageDownloadWorker:
        if not self._enable_image_worker:
            raise RuntimeError("Image worker disabled")
        if not self._image_worker:
            self._image_worker = ImageDownloadWorker(
                log_error=self._log_download_error,
                pg_dsn=self._pg_dsn,
                workers=self._image_workers,
            )
        return self._image_worker

    def wait_for_images(self) -> None:
        if self._image_worker:
            self._image_worker.wait()

    def wait_for_images_with_progress(self, *, label: str = "下载图片") -> None:
        if not self._image_worker:
            return
        try:
            from tqdm import tqdm
        except Exception:
            self._image_worker.wait()
            return
        total, done = self._image_worker.stats()
        if total == 0:
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
                total, done = self._image_worker.stats()
                if total > bar.total:
                    bar.total = total
                bar.n = done
                bar.refresh()
                if done >= total:
                    break
                time.sleep(0.2)
        except KeyboardInterrupt:
            self.abort_image_queue()
            raise
        finally:
            bar.close()
        self._image_worker.wait()

    def abort_image_queue(self) -> None:
        if self._image_worker:
            self._image_worker.mark_pending_as_failed()

    def _retry_failed_images(self) -> None:
        tasks = _load_failed_image_tasks()
        if not tasks:
            return
        worker = self._ensure_image_worker()
        for task in tasks:
            worker.enqueue(task)

    # ------------------------------------------------------------------
    def download_many(
        self,
        articles: Iterable[ArticleRecord],
        *,
        fmt: str = "html",
        with_images: bool = True,
        record_images_only: bool = False,
        account_name: Optional[str] = None,
        progress: Optional[object] = None,
        skip_if_downloaded: bool = True,
    ) -> tuple[List[DownloadResult], int, int]:
        """Download multiple articles.
        
        Returns:
            tuple of (results, skipped_count, failed_count)
        """
        results: List[DownloadResult] = []
        skipped = 0
        failed = 0
        pending: List[ArticleRecord] = []
        articles_list = list(articles)
        content_ids: Optional[set[str]] = None
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
                    if self._is_downloaded(article, account_name):
                        skipped += 1
                        if progress is not None:
                            progress.update(1)
                        continue
            pending.append(article)

        if not pending:
            return results, skipped, failed

        def download_one(target: ArticleRecord) -> DownloadResult:
            return self._download_with_retry(
                target,
                fmt=fmt,
                with_images=with_images,
                record_images_only=record_images_only,
                account_name=account_name,
            )

        if self._article_workers <= 1:
            try:
                for article in pending:
                    try:
                        result = download_one(article)
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
                self.abort_image_queue()
                raise
            return results, skipped, failed

        with concurrent.futures.ThreadPoolExecutor(
            max_workers=self._article_workers
        ) as executor:
            future_map = {
                executor.submit(download_one, article): article for article in pending
            }
            try:
                for future in concurrent.futures.as_completed(future_map):
                    article = future_map[future]
                    try:
                        result = future.result()
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
            except KeyboardInterrupt:
                self.abort_image_queue()
                for future in future_map:
                    future.cancel()
                executor.shutdown(wait=False, cancel_futures=True)
                raise
        return results, skipped, failed

    def download_from_url(
        self,
        url: str,
        *,
        fmt: str = "html",
        with_images: bool = True,
        record_images_only: bool = False,
        title: Optional[str] = None,
    ) -> DownloadResult:
        raw_html = self._fetch_with_retry(url)
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
        return self._persist_article(
            stub,
            raw_html=raw_html,
            fmt=fmt,
            with_images=with_images,
            record_images_only=record_images_only,
            account_name=None,
        )

    def _fetch_with_retry(self, url: str) -> str:
        last_exc: Optional[Exception] = None
        for attempt in range(_ARTICLE_MAX_RETRIES):
            try:
                logger.debug("Fetching article (attempt %d/%d): %s", attempt + 1, _ARTICLE_MAX_RETRIES, url)
                return self.client.fetch_article_html(url)
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
                    time.sleep(wait_time)
        
        logger.error("Failed to fetch article after %d retries: %s", _ARTICLE_MAX_RETRIES, url)
        raise RuntimeError(f"下载失败：{last_exc}") from last_exc

    def _download_with_retry(
        self,
        article: ArticleRecord,
        *,
        fmt: str,
        with_images: bool,
        record_images_only: bool,
        account_name: Optional[str],
    ) -> DownloadResult:
        last_exc: Optional[Exception] = None
        for attempt in range(_ARTICLE_MAX_RETRIES):
            try:
                logger.debug(
                    "Downloading article (attempt %d/%d): %s - %s",
                    attempt + 1,
                    _ARTICLE_MAX_RETRIES,
                    article.article_id,
                    article.title,
                )
                html = self.client.fetch_article_html(article.link)
                result = self._persist_article(
                    article,
                    raw_html=html,
                    fmt=fmt,
                    with_images=with_images,
                    record_images_only=record_images_only,
                    account_name=account_name,
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
                    time.sleep(wait_time)
        
        logger.error("Failed to download article after %d retries: %s - %s", _ARTICLE_MAX_RETRIES, article.article_id, article.title)
        raise RuntimeError(f"下载失败：{last_exc}") from last_exc

    # ------------------------------------------------------------------
    def _persist_article(
        self,
        article: ArticleRecord,
        *,
        raw_html: str,
        fmt: str,
        with_images: bool,
        record_images_only: bool,
        account_name: Optional[str],
    ) -> DownloadResult:
        target_dir = self._article_target_dir(article, account_name, create=True)

        clean_html = normalize_html(raw_html, fmt="html")
        normalized_html = clean_html
        asset_count = 0
        url_map: dict[str, str] = {}
        referer = article.link or "https://mp.weixin.qq.com/"
        if with_images:
            normalized_html, asset_count, url_map = self._download_images(
                normalized_html, target_dir, referer=referer, article=article
            )
        elif record_images_only:
            asset_count, url_map = self._collect_image_urls(
                normalized_html, referer=referer
            )

        output_path = target_dir / _FORMAT_EXTENSIONS["html"]
        markdown_path = target_dir / _FORMAT_EXTENSIONS["markdown"]
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
        text_path = None
        local_error: Optional[Exception] = None
        try:
            output_path.write_text(normalized_html, encoding="utf-8")
            markdown_path.write_text(markdown_content, encoding="utf-8")

            if fmt == "text":
                text_path = target_dir / _FORMAT_EXTENSIONS["text"]
                text_content = normalize_html(raw_html, fmt="text")
                text_path.write_text(text_content, encoding="utf-8")
            elif fmt not in ("html", "markdown"):
                raise ValueError(f"Unsupported format: {fmt}")

            metadata = {
                "title": article.title,
                "link": article.link,
                "source_url": article.source_url,
                "author": article.author,
                "digest": article.digest,
                "publish_at": article.publish_at,
                "html_path": _FORMAT_EXTENSIONS["html"],
                "markdown_path": _FORMAT_EXTENSIONS["markdown"],
                "text_path": _FORMAT_EXTENSIONS["text"] if text_path else None,
                "assets": asset_count,
            }
            (target_dir / "metadata.json").write_text(
                json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception as exc:
            local_error = exc

        pg_error: Optional[Exception] = None
        try:
            self._store_article_pg(
                article=article,
                clean_html=clean_html,
                markdown_content=markdown_content,
                target_dir=target_dir,
                url_map=url_map,
            )
        except Exception as exc:
            pg_error = exc

        if local_error:
            raise RuntimeError(f"Local write failed: {local_error}") from local_error
        if pg_error:
            raise pg_error

        return DownloadResult(article=article, output_path=str(output_path), asset_count=asset_count)

    def _store_article_pg(
        self,
        *,
        article: ArticleRecord,
        clean_html: str,
        markdown_content: str,
        target_dir: Path,
        url_map: dict[str, str],
    ) -> None:
        if not self.storage:
            return
        if not hasattr(self.storage, "save_article_content"):
            return
        title, cover_local, blocks, body_markdown = _parse_markdown_blocks(markdown_content)
        base_url = article.link or "https://mp.weixin.qq.com/"
        local_to_orig = {
            local: _resolve_asset_url(orig, base=base_url) or orig
            for orig, local in url_map.items()
        }
        content_markdown = body_markdown
        for local_path, orig_url in local_to_orig.items():
            content_markdown = content_markdown.replace(f"]({local_path})", f"]({orig_url})")
        cover_url = None
        if cover_local:
            cover_url = local_to_orig.get(cover_local, cover_local)
        elif article.cover:
            cover_url = article.cover
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
            orig_url = local_to_orig.get(local_path, local_path)
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
                orig_url = local_to_orig.get(local_path, local_path)
                updated = dict(block)
                updated.pop("local_path", None)
                updated["orig_url"] = orig_url
                blocks_with_urls.append(updated)
            else:
                blocks_with_urls.append(block)

        self.storage.save_article_content(
            article,
            url_token=url_token,
            title=title or article.title,
            clean_html=clean_html,
            content_markdown=content_markdown,
            content_blocks=blocks_with_urls,
            cover_url=cover_url,
            images=images,
        )

    def _article_target_dir(
        self, article: ArticleRecord, account_name: Optional[str], *, create: bool
    ) -> Path:
        base_segment = _article_base_segment(article)
        if account_name:
            base_dir = self.output_dir / slugify(account_name)
        else:
            base_dir = self.output_dir
        if not create:
            return base_dir / base_segment
        def allocate_path(segment: str) -> Path:
            target = base_dir / segment
            if not target.exists():
                return ensure_directory(target)
            index = 2
            while True:
                candidate = base_dir / f"{segment}-{index}"
                if not candidate.exists():
                    return ensure_directory(candidate)
                index += 1

        try:
            return allocate_path(base_segment)
        except OSError as exc:
            if exc.errno != errno.ENAMETOOLONG:
                raise
            fallback_segment = f"{timestamp_to_datestr(article.publish_at)}-{article.article_id or 'article'}"
            fallback_segment = _truncate_utf8(fallback_segment, 120) or "article"
            return allocate_path(fallback_segment)

    def _has_local_files(self, article: ArticleRecord, account_name: Optional[str]) -> bool:
        account_segment = slugify(account_name) if account_name else ""
        base_segment = _article_base_segment(article)
        base_dir = self.output_dir / account_segment if account_segment else self.output_dir
        if not base_dir.exists():
            return False
        for candidate in base_dir.iterdir():
            if not candidate.is_dir():
                continue
            name = candidate.name
            if name == base_segment:
                pass
            else:
                match = _SUFFIX_PATTERN.match(name)
                if not match or match.group("base") != base_segment:
                    continue
            metadata_path = candidate / "metadata.json"
            if metadata_path.exists():
                try:
                    payload = json.loads(metadata_path.read_text(encoding="utf-8"))
                except Exception:
                    payload = {}
                link = payload.get("link")
                if link and article.link and link == article.link:
                    return True
            html_path = candidate / _FORMAT_EXTENSIONS["html"]
            md_path = candidate / _FORMAT_EXTENSIONS["markdown"]
            if html_path.exists() and md_path.exists() and not article.link:
                return True
        return False

    def _is_downloaded(self, article: ArticleRecord, account_name: Optional[str]) -> bool:
        if self.storage and os.environ.get("HIPPO_PG_DSN"):
            has_content = getattr(self.storage, "has_article_content", None)
            if callable(has_content):
                try:
                    return bool(has_content(article.biz, article.article_id))
                except Exception:
                    return False
        return self._has_local_files(article, account_name)

    def _collect_image_urls(
        self, html: str, *, referer: str
    ) -> tuple[int, dict[str, str]]:
        soup = BeautifulSoup(html, "html.parser")
        count = 0
        url_map: dict[str, str] = {}

        def add_url(url: str, *, prefix: str) -> None:
            nonlocal count
            resolved = _resolve_asset_url(url, base=referer)
            if not resolved:
                return
            if url in url_map:
                return
            extension = _guess_extension(resolved) or ".bin"
            count += 1
            filename = f"{prefix}_{count:03d}{extension}"
            url_map[url] = f"images/{filename}"

        for img in soup.find_all("img"):
            src = img.get("src") or img.get("data-src")
            if not src or src.startswith("data:"):
                continue
            add_url(src, prefix="img")

        def extract_from_style(text: str) -> None:
            for match in _URL_PATTERN.finditer(text):
                candidate = match.group("url").strip()
                if not candidate or candidate.startswith("data:"):
                    continue
                add_url(candidate, prefix="asset")

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

    def _download_images(
        self, html: str, target_dir: Path, *, referer: str, article: ArticleRecord
    ) -> tuple[str, int, dict[str, str]]:
        soup = BeautifulSoup(html, "html.parser")
        count = 0
        url_map: dict[str, str] = {}
        worker = self._ensure_image_worker()

        def download_asset(url: str, *, prefix: str) -> str | None:
            nonlocal count
            resolved = _resolve_asset_url(url, base=referer)
            if not resolved:
                return None
            if url in url_map:
                return url_map[url]
            extension = _guess_extension(resolved) or ".bin"
            count += 1
            filename = f"{prefix}_{count:03d}{extension}"
            local_path = f"images/{filename}"
            url_map[url] = local_path
            worker.enqueue(
                {
                    "article": article,
                    "resolved_url": resolved,
                    "orig_url": url,
                    "referer": referer,
                    "target_dir": target_dir,
                    "local_path": local_path,
                }
            )
            return local_path

        def rewrite_css_urls(text: str) -> str:
            def replacer(match: re.Match[str]) -> str:
                url = match.group("url").strip()
                if not url or url.startswith("data:"):
                    return match.group(0)
                local = download_asset(url, prefix="asset")
                if not local:
                    return match.group(0)
                return f"url('{local}')"

            return _URL_PATTERN.sub(replacer, text)

        for img in soup.find_all("img"):
            src = img.get("src") or img.get("data-src")
            if not src or src.startswith("data:"):
                continue
            local = download_asset(src, prefix="img")
            if local:
                img["src"] = local

        for node in soup.find_all(style=True):
            style = node.get("style") or ""
            if "url(" not in style:
                continue
            node["style"] = rewrite_css_urls(style)

        for style_tag in soup.find_all("style"):
            if not style_tag.string:
                continue
            if "url(" not in style_tag.string:
                continue
            style_tag.string.replace_with(rewrite_css_urls(style_tag.string))

        return soup.decode(), count, url_map

    def _log_download_error(
        self,
        *,
        stage: str,
        article: ArticleRecord,
        error: str,
        asset_url: Optional[str] = None,
        resolved_url: Optional[str] = None,
        referer: Optional[str] = None,
        target_dir: Optional[Path] = None,
        local_path: Optional[str] = None,
    ) -> None:
        log_dir = ensure_directory(HOME_DIR / "logs")
        log_path = log_dir / _ERROR_LOG_NAME
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
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
        if target_dir:
            payload["target_dir"] = str(target_dir)
        if local_path:
            payload["local_path"] = local_path
        try:
            with log_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
        except Exception:
            pass


# Helper functions ---------------------------------------------------------

def _guess_extension(url: str) -> str:
    parsed = urlparse(url)
    suffix = Path(parsed.path).suffix
    if suffix:
        return suffix
    content_type = mimetypes.guess_type(url)[0]
    if content_type:
        return mimetypes.guess_extension(content_type) or ""
    return ""


def _extract_title(raw_html: str) -> Optional[str]:
    soup = BeautifulSoup(raw_html, "html.parser")
    if soup.title and soup.title.string:
        return soup.title.string.strip()
    h1 = soup.find("h1")
    if h1 and h1.get_text(strip=True):
        return h1.get_text(strip=True)
    return None


def _load_failed_image_tasks() -> list[dict]:
    log_path = HOME_DIR / "logs" / _ERROR_LOG_NAME
    if not log_path.exists():
        return []
    tasks: list[dict] = []
    seen: set[tuple[str, str]] = set()
    try:
        with log_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if payload.get("stage") != "asset_download":
                    continue
                target_dir = payload.get("target_dir")
                local_path = payload.get("local_path")
                resolved_url = payload.get("resolved_url") or payload.get("asset_url")
                referer = payload.get("referer")
                article_info = payload.get("article") or {}
                if not target_dir or not local_path or not resolved_url:
                    continue
                key = (target_dir, local_path)
                if key in seen:
                    continue
                seen.add(key)
                file_path = Path(target_dir) / local_path
                if file_path.exists() and file_path.stat().st_size > 0:
                    continue
                article = ArticleRecord(
                    biz=article_info.get("biz") or "unknown",
                    article_id=article_info.get("article_id") or local_path,
                    title=article_info.get("title") or "Unknown",
                    author=None,
                    digest=None,
                    cover=None,
                    link=article_info.get("link") or referer or "",
                    source_url=article_info.get("link") or referer or "",
                    publish_at=None,
                    raw={"source": "retry"},
                )
                tasks.append(
                    {
                        "article": article,
                        "resolved_url": resolved_url,
                        "orig_url": payload.get("asset_url"),
                        "referer": referer,
                        "target_dir": Path(target_dir),
                        "local_path": local_path,
                    }
                )
    except Exception:
        return tasks
    return tasks


__all__ = ["ArticleDownloader"]
