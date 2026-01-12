"""Utilities for downloading article HTML and assets."""

from __future__ import annotations

import json
import os
import mimetypes
import queue
import re
import threading
import time
from typing import Callable, Dict, Tuple
from datetime import datetime, timezone
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Iterable, List, Optional
from urllib.parse import urlparse, urljoin

from bs4 import BeautifulSoup

try:
    # Running from repo root: python/ is a package.
    from ..normalize_html import normalize_html
except ImportError:  # pragma: no cover - fallback for running inside python/
    from normalize_html import normalize_html

from .config import DOWNLOAD_ROOT, HOME_DIR
from .http import MPClient
from .models import ArticleRecord, DownloadResult
from .storage import StorageLike
from .utils import ensure_directory, slugify, timestamp_to_datestr

_FORMAT_EXTENSIONS = {
    "html": "index.html",
    "markdown": "article.md",
    "text": "article.txt",
}
_URL_PATTERN = re.compile(r"url\((?P<quote>[\"']?)(?P<url>[^\"')]+)(?P=quote)\)", re.IGNORECASE)


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


def _resolve_asset_url(url: str, *, base: str) -> Optional[str]:
    if not url:
        return None
    lowered = url.strip().lower()
    if lowered.startswith(("data:", "javascript:", "about:")):
        return None
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
        return None
    if not parsed_resolved.path or parsed_resolved.path == "/":
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


class ImageDownloadWorker:
    def __init__(
        self,
        *,
        log_error: Callable[..., None],
        pg_dsn: Optional[str],
    ) -> None:
        self._log_error = log_error
        self._pg_dsn = pg_dsn
        self._queue: "queue.Queue[Optional[dict]]" = queue.Queue()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def enqueue(self, task: dict) -> None:
        self._queue.put(task)

    def close(self) -> None:
        self._queue.join()
        self._queue.put(None)
        self._thread.join()

    def _run(self) -> None:
        storage = None
        if self._pg_dsn:
            from .storage import PostgresStorage

            storage = PostgresStorage(self._pg_dsn)
        with MPClient() as client:
            while True:
                task = self._queue.get()
                if task is None:
                    self._queue.task_done()
                    break
                article = task["article"]
                resolved_url = task["resolved_url"]
                orig_url = task.get("orig_url")
                referer = task.get("referer")
                target_dir = task["target_dir"]
                local_path = task["local_path"]
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
                except Exception as exc:
                    self._log_error(
                        stage="asset_download",
                        article=article,
                        error=str(exc),
                        asset_url=orig_url or resolved_url,
                        resolved_url=resolved_url,
                        referer=referer,
                    )
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
    ) -> None:
        self._managed_client = client is None
        self.client = client or MPClient(
            article_worker=article_worker,
            article_worker_proxy=article_worker_proxy,
            article_max_connections=article_max_connections,
        )
        self.output_dir = ensure_directory(output_dir or DOWNLOAD_ROOT)
        self.storage = storage
        self._pg_dsn = os.environ.get("WECHATCLI_PG_DSN")
        self._image_worker: Optional[ImageDownloadWorker] = None

    def __enter__(self) -> "ArticleDownloader":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:  # type: ignore[override]
        self.close()

    def close(self) -> None:
        if self._image_worker:
            self._image_worker.close()
        if self._managed_client:
            self.client.close()

    def _ensure_image_worker(self) -> ImageDownloadWorker:
        if not self._image_worker:
            self._image_worker = ImageDownloadWorker(
                log_error=self._log_download_error,
                pg_dsn=self._pg_dsn,
            )
        return self._image_worker

    # ------------------------------------------------------------------
    def download_many(
        self,
        articles: Iterable[ArticleRecord],
        *,
        fmt: str = "html",
        with_images: bool = True,
        account_name: Optional[str] = None,
        progress: Optional[object] = None,
        skip_if_downloaded: bool = True,
    ) -> tuple[List[DownloadResult], int]:
        results: List[DownloadResult] = []
        skipped = 0
        for article in articles:
            if skip_if_downloaded and self._is_downloaded(article, account_name):
                skipped += 1
                if progress is not None:
                    progress.update(1)
                continue
            try:
                result = self._download_with_retry(
                    article,
                    fmt=fmt,
                    with_images=with_images,
                    account_name=account_name,
                )
                results.append(result)
            except Exception as exc:
                self._log_download_error(
                    stage="article_download",
                    article=article,
                    error=str(exc),
                )
                if progress is not None:
                    progress.write(f"下载失败：{article.title} ({article.article_id}) {exc}")
                    progress.update(1)
                continue
            if progress is not None:
                progress.update(1)
        return results, skipped

    def download_from_url(
        self,
        url: str,
        *,
        fmt: str = "html",
        with_images: bool = True,
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
            account_name="adhoc",
        )

    def _fetch_with_retry(self, url: str) -> str:
        last_exc: Optional[Exception] = None
        for attempt in range(3):
            try:
                return self.client.fetch_article_html(url)
            except Exception as exc:
                last_exc = exc
                time.sleep(min(2 ** attempt, 5))
        raise RuntimeError(f"下载失败：{last_exc}") from last_exc

    def _download_with_retry(
        self,
        article: ArticleRecord,
        *,
        fmt: str,
        with_images: bool,
        account_name: Optional[str],
    ) -> DownloadResult:
        last_exc: Optional[Exception] = None
        for attempt in range(3):
            try:
                html = self.client.fetch_article_html(article.link)
                return self._persist_article(
                    article,
                    raw_html=html,
                    fmt=fmt,
                    with_images=with_images,
                    account_name=account_name,
                )
            except Exception as exc:
                last_exc = exc
                time.sleep(min(2 ** attempt, 5))
        raise RuntimeError(f"下载失败：{last_exc}") from last_exc

    # ------------------------------------------------------------------
    def _persist_article(
        self,
        article: ArticleRecord,
        *,
        raw_html: str,
        fmt: str,
        with_images: bool,
        account_name: Optional[str],
    ) -> DownloadResult:
        target_dir = self._article_target_dir(article, account_name, create=True)

        clean_html = normalize_html(raw_html, fmt="html")
        normalized_html = clean_html
        asset_count = 0
        url_map: dict[str, str] = {}
        if with_images:
            referer = article.link or "https://mp.weixin.qq.com/"
            normalized_html, asset_count, url_map = self._download_images(
                normalized_html, target_dir, referer=referer, article=article
            )

        output_path = target_dir / _FORMAT_EXTENSIONS["html"]
        output_path.write_text(normalized_html, encoding="utf-8")

        markdown_path = target_dir / _FORMAT_EXTENSIONS["markdown"]
        markdown_content = normalize_html(
            raw_html, fmt="markdown", markdown_image_map=url_map
        )
        markdown_path.write_text(markdown_content, encoding="utf-8")

        text_path = None
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
        self._store_article_pg(
            article=article,
            clean_html=clean_html,
            markdown_content=markdown_content,
            target_dir=target_dir,
            url_map=url_map,
        )
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
        account_segment = slugify(account_name or article.biz or "account")
        title_segment = slugify(article.title) or article.article_id or "article"
        article_segment = f"{timestamp_to_datestr(article.publish_at)}-{title_segment}"
        target = self.output_dir / account_segment / article_segment
        return ensure_directory(target) if create else target

    def _is_downloaded(self, article: ArticleRecord, account_name: Optional[str]) -> bool:
        if self.storage and os.environ.get("WECHATCLI_PG_DSN"):
            has_content = getattr(self.storage, "has_article_content", None)
            if callable(has_content):
                try:
                    return bool(has_content(article.biz, article.article_id))
                except Exception:
                    return False
        target_dir = self._article_target_dir(article, account_name, create=False)
        html_path = target_dir / _FORMAT_EXTENSIONS["html"]
        md_path = target_dir / _FORMAT_EXTENSIONS["markdown"]
        return html_path.exists() and md_path.exists()

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
    ) -> None:
        log_dir = ensure_directory(HOME_DIR / "logs")
        log_path = log_dir / "download_errors.jsonl"
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


__all__ = ["ArticleDownloader"]
