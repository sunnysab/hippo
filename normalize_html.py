#!/usr/bin/env python3
"""Normalize WeChat article HTML into cleaned HTML/text/markdown outputs."""

from __future__ import annotations

import argparse
import re
from pathlib import Path
from typing import Dict, Tuple

from bs4 import BeautifulSoup
from markdownify import markdownify

REMOVE_SELECTORS = [
    '#js_top_ad_area',
    '#js_tags_preview_toast',
    '#content_bottom_area',
    '#js_pc_qr_code',
    '#wx_stream_article_slide_tip',
]

STYLE_BLOCK = """
        #js_row_immersive_stream_wrap {
            max-width: 667px;
            margin: 0 auto;
        }
        #js_row_immersive_stream_wrap .wx_follow_avatar_pic {
          display: block;
          margin: 0 auto;
        }
        #page-content,
        #js_article_bottom_bar,
        .__page_content__ {
            max-width: 667px;
            margin: 0 auto;
        }
        img {
            max-width: 100%;
        }
        .sns_opr_btn::before {
            width: 16px;
            height: 16px;
            margin-right: 3px;
        }
"""


def _prepare_dom(raw_html: str) -> Tuple[BeautifulSoup, BeautifulSoup]:
    soup = BeautifulSoup(raw_html, 'html.parser')
    js_article = soup.find(id='js_article') or soup

    js_content = js_article.find(id='js_content')
    if js_content and js_content.has_attr('style'):
        del js_content['style']

    for selector in REMOVE_SELECTORS:
        for node in js_article.select(selector):
            node.decompose()

    for script in js_article.find_all('script'):
        script.decompose()

    for img in soup.find_all('img'):
        src = img.get('src') or img.get('data-src')
        if src:
            img['src'] = src

    return soup, js_article


def _render_html(soup: BeautifulSoup, js_article: BeautifulSoup) -> str:
    body_cls = ''
    if soup.body and soup.body.has_attr('class'):
        body_cls = ' '.join(soup.body['class'])

    page_content_html = str(js_article)

    return (
        '<!DOCTYPE html>\n'
        '<html lang="zh_CN">\n'
        '<head>\n'
        '    <meta charset="utf-8">\n'
        '    <meta http-equiv="Content-Type" content="text/html; charset=utf-8">\n'
        '    <meta http-equiv="X-UA-Compatible" content="IE=edge">\n'
        '    <meta name="viewport" content="width=device-width,initial-scale=1.0,maximum-scale=1.0,user-scalable=0,viewport-fit=cover">\n'
        '    <meta name="referrer" content="no-referrer">\n'
        '    <style>'
        f'{STYLE_BLOCK}'
        '    </style>\n'
        '</head>\n'
        f'<body class="{body_cls}">\n'
        f'{page_content_html}\n'
        '</body>\n'
        '</html>\n'
    )


def _postprocess_markdown(markdown: str) -> str:
    lines = [line.rstrip() for line in markdown.splitlines()]
    processed: list[str] = []
    image_pattern = re.compile(r'!\[([^\]]*)\]\(([^)]+)\)')
    empty_image_pattern = re.compile(r'^!\[[^\]]*]\(\s*\)$')
    js_void_pattern = re.compile(r'\]\(\s*javascript:void\(0\);?\s*\)', re.IGNORECASE)
    immersive_tip = '在小说阅读器中沉浸阅读'
    prev_blank = False
    for line in lines:
        stripped = line.strip()
        if not stripped:
            if prev_blank:
                continue
            prev_blank = True
            processed.append('')
            continue
        prev_blank = False
        rebuilt = []
        last_end = 0
        found = False
        for match in image_pattern.finditer(line):
            found = True
            if match.start() > last_end:
                chunk = line[last_end:match.start()].strip()
                if chunk:
                    rebuilt.append(chunk)
            alt, url = match.groups()
            rebuilt.append(f'![{alt}]({url})')
            last_end = match.end()
        if found:
            tail = line[last_end:].strip()
            if tail:
                rebuilt.append(tail)
            for part in rebuilt:
                processed.append(part)
            continue
        processed.append(line.strip())

    def is_plain_text_line(text: str) -> bool:
        if not text:
            return False
        if text.startswith(('#', '-', '*', '+', '>', '|')):
            return False
        if text.startswith('!['):
            return False
        if re.search(r'\[[^\]]+]\([^)]*\)', text):
            return False
        return True

    cleaned: list[str] = []
    for line in processed:
        stripped = line.strip()
        if stripped == immersive_tip:
            continue
        if empty_image_pattern.match(stripped):
            continue
        cleaned.append(line)

    to_remove: set[int] = set()
    non_empty = [i for i, line in enumerate(cleaned) if line.strip()]
    js_void_lines = [i for i, line in enumerate(cleaned) if js_void_pattern.search(line)]
    for idx in js_void_lines:
        to_remove.add(idx)
        if idx in non_empty:
            ordinal = non_empty.index(idx) + 1
        else:
            ordinal = len(non_empty) + 1
        if ordinal <= 8:
            prev_idx = idx - 1
            while prev_idx >= 0 and not cleaned[prev_idx].strip():
                prev_idx -= 1
            if prev_idx >= 0:
                prev_line = cleaned[prev_idx].strip()
                if is_plain_text_line(prev_line):
                    to_remove.add(prev_idx)
            else:
                for candidate in non_empty:
                    candidate_line = cleaned[candidate].strip()
                    if is_plain_text_line(candidate_line):
                        to_remove.add(candidate)
                        break
    if js_void_lines and non_empty:
        first_plain_idx = None
        for candidate in non_empty:
            candidate_line = cleaned[candidate].strip()
            if is_plain_text_line(candidate_line):
                first_plain_idx = candidate
                break
        if first_plain_idx is not None and first_plain_idx not in to_remove:
            first_js_void_ordinal = min(non_empty.index(idx) + 1 for idx in js_void_lines if idx in non_empty)
            if first_js_void_ordinal <= 8:
                to_remove.add(first_plain_idx)

    final_lines = [line for i, line in enumerate(cleaned) if i not in to_remove]
    return '\n'.join(final_lines).strip()


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


def normalize_html(raw_html: str, fmt: str = 'html', *, markdown_image_map: Dict[str, str] | None = None) -> str:
    soup, js_article = _prepare_dom(raw_html)

    if fmt == 'text':
        text = js_article.get_text(separator='\n')
        lines = [line.strip() for line in text.splitlines()]
        filtered = [line for line in lines if line]
        return '\n'.join(filtered)
    if fmt == 'html':
        return _render_html(soup, js_article)
    if fmt == 'markdown':
        cleaned_html = _render_html(soup, js_article)
        markdown = markdownify(cleaned_html, heading_style='ATX')
        if markdown_image_map:
            markdown = _swap_markdown_image_urls(markdown, markdown_image_map)
        return _postprocess_markdown(markdown)
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
