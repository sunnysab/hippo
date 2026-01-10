#!/usr/bin/env python3
"""Normalize WeChat article HTML into cleaned HTML/text/markdown outputs."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Tuple

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


def normalize_html(raw_html: str, fmt: str = 'html') -> str:
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
        return markdownify(cleaned_html, heading_style='ATX')
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
