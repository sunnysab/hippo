#!/usr/bin/env python3
"""Normalize WeChat article HTML into cleaned HTML/text/markdown outputs."""

from __future__ import annotations

import argparse
import re
from pathlib import Path
from typing import Dict

from bs4 import BeautifulSoup
from html2text import HTML2Text
from readability import Document

REMOVE_SELECTORS = [
    '#js_top_ad_area',
    '#js_tags_preview_toast',
    '#content_bottom_area',
    '#js_pc_qr_code',
    '#wx_stream_article_slide_tip',
]

def _extract_readable_content(raw_html: str) -> BeautifulSoup:
    doc = Document(raw_html)
    content_html = doc.summary(html_partial=True)
    soup = BeautifulSoup(content_html, 'html.parser')
    for selector in REMOVE_SELECTORS:
        for node in soup.select(selector):
            node.decompose()
    for script in soup.find_all('script'):
        script.decompose()
    for img in soup.find_all('img'):
        src = img.get('src') or img.get('data-src')
        if src:
            img['src'] = src
    return soup


def _render_html(content: BeautifulSoup) -> str:
    page_content_html = str(content)
    return (
        '<!DOCTYPE html>\n'
        '<html lang="zh_CN">\n'
        '<head>\n'
        '    <meta charset="utf-8">\n'
        '    <meta http-equiv="Content-Type" content="text/html; charset=utf-8">\n'
        '    <meta http-equiv="X-UA-Compatible" content="IE=edge">\n'
        '    <meta name="viewport" content="width=device-width,initial-scale=1.0,maximum-scale=1.0,user-scalable=0,viewport-fit=cover">\n'
        '    <meta name="referrer" content="no-referrer">\n'
        '</head>\n'
        '<body>\n'
        f'{page_content_html}\n'
        '</body>\n'
        '</html>\n'
    )


def _swap_markdown_image_urls(markdown: str, url_map: Dict[str, str]) -> str:
    if not url_map:
        return markdown
    def replacer(match: re.Match[str]) -> str:
        alt, url = match.group(1), match.group(2)
        local = url_map.get(url)
        if local:
            return f'![{alt}]({local})'
        return match.group(0)
    return re.sub(r'!\[([^\]]*)\]\(([^)]+)\)', replacer, markdown)


def _render_markdown(html: str) -> str:
    converter = HTML2Text()
    converter.body_width = 0
    converter.ignore_links = False
    converter.ignore_images = False
    markdown = converter.handle(html)
    return markdown.strip()


def normalize_html(raw_html: str, fmt: str = 'html', *, markdown_image_map: Dict[str, str] | None = None) -> str:
    content = _extract_readable_content(raw_html)

    if fmt == 'text':
        text = content.get_text(separator='\n')
        lines = [line.strip() for line in text.splitlines()]
        filtered = [line for line in lines if line]
        return '\n'.join(filtered)
    if fmt == 'html':
        return _render_html(content)
    if fmt == 'markdown':
        cleaned_html = _render_html(content)
        markdown = _render_markdown(cleaned_html)
        if markdown_image_map:
            markdown = _swap_markdown_image_urls(markdown, markdown_image_map)
        return markdown
    raise ValueError(f'Unsupported format: {fmt}')


def main() -> None:
    parser = argparse.ArgumentParser(description='Normalize WeChat article HTML file.')
    parser.add_argument('--input', '-i', required=True, help='Path to the HTML file to process')
    parser.add_argument(
        '--format',
        '-f',
        default='html',
        choices=['html', 'text', 'markdown'],
        help='Output format (default: html)',
    )

    args = parser.parse_args()
    raw_html = Path(args.input).read_text(encoding='utf-8')
    result = normalize_html(raw_html, args.format)
    print(result)


if __name__ == '__main__':
    main()
