"""Utilities for downloading article HTML and assets."""

from __future__ import annotations

import json
import mimetypes
import re
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Iterable, List, Optional
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

try:
    # Running from repo root: python/ is a package.
    from ..normalize_html import normalize_html
except ImportError:  # pragma: no cover - fallback for running inside python/
    from normalize_html import normalize_html

from .config import DOWNLOAD_ROOT
from .http import MPClient
from .models import ArticleRecord, DownloadResult
from .utils import ensure_directory, slugify, timestamp_to_datestr

_FORMAT_EXTENSIONS = {
    "html": "index.html",
    "markdown": "article.md",
    "text": "article.txt",
}
_URL_PATTERN = re.compile(r"url\((?P<quote>[\"']?)(?P<url>[^\"')]+)(?P=quote)\)", re.IGNORECASE)


class ArticleDownloader(AbstractContextManager):
    def __init__(
        self,
        *,
        client: Optional[MPClient] = None,
        output_dir: Optional[Path] = None,
    ) -> None:
        self._managed_client = client is None
        self.client = client or MPClient()
        self.output_dir = ensure_directory(output_dir or DOWNLOAD_ROOT)

    def __enter__(self) -> "ArticleDownloader":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:  # type: ignore[override]
        self.close()

    def close(self) -> None:
        if self._managed_client:
            self.client.close()

    # ------------------------------------------------------------------
    def download_many(
        self,
        articles: Iterable[ArticleRecord],
        *,
        fmt: str = "html",
        with_images: bool = True,
        account_name: Optional[str] = None,
    ) -> List[DownloadResult]:
        results: List[DownloadResult] = []
        for article in articles:
            html = self.client.fetch_article_html(article.link)
            result = self._persist_article(
                article,
                raw_html=html,
                fmt=fmt,
                with_images=with_images,
                account_name=account_name,
            )
            results.append(result)
        return results

    def download_from_url(
        self,
        url: str,
        *,
        fmt: str = "html",
        with_images: bool = True,
        title: Optional[str] = None,
    ) -> DownloadResult:
        raw_html = self.client.fetch_article_html(url)
        inferred_title = title or _extract_title(raw_html) or "WeChat Article"
        stub = ArticleRecord(
            biz="adhoc",
            article_id=slugify(inferred_title),
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
        account_segment = slugify(account_name or article.biz or "account")
        article_segment = (
            f"{timestamp_to_datestr(article.publish_at)}-"
            f"{account_segment}-"
            f"{slugify(article.title)}"
        )
        target_dir = ensure_directory(self.output_dir / account_segment / article_segment)

        normalized_html = normalize_html(raw_html, fmt="html")
        asset_count = 0
        url_map: dict[str, str] = {}
        if with_images:
            normalized_html, asset_count, url_map = self._download_images(
                normalized_html, target_dir, referer=article.link
            )

        output_path = target_dir / _FORMAT_EXTENSIONS["html"]
        output_path.write_text(normalized_html, encoding="utf-8")

        markdown_path = target_dir / _FORMAT_EXTENSIONS["markdown"]
        markdown_content = normalize_html(raw_html, fmt="markdown", markdown_image_map=url_map)
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
        return DownloadResult(article=article, output_path=str(output_path), asset_count=asset_count)

    def _download_images(
        self, html: str, target_dir: Path, *, referer: str
    ) -> tuple[str, int, dict[str, str]]:
        soup = BeautifulSoup(html, "html.parser")
        images_dir = ensure_directory(target_dir / "images")
        count = 0
        url_map: dict[str, str] = {}

        def download_asset(url: str, *, prefix: str) -> str | None:
            nonlocal count
            if url in url_map:
                return url_map[url]
            try:
                data = self.client.download_binary(url, referer=referer)
            except httpx.HTTPError:
                return None
            extension = _guess_extension(url) or ".bin"
            count += 1
            filename = f"{prefix}_{count:03d}{extension}"
            (images_dir / filename).write_bytes(data)
            local_path = f"images/{filename}"
            url_map[url] = local_path
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
