"""Structured WeChat article parser built around ``window.cgiDataNew``."""

from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlparse

import quickjs
from bs4 import BeautifulSoup
from markdownify import markdownify

from .logger import get_logger

logger = get_logger(__name__)

_JS_EVAL_TIMEOUT_MS = 8_000
_TITLE_FALLBACK = '(untitled)'
_SKIPPED_SCRIPT_MARKERS = ('import.meta',)
_EMBED_PLACEHOLDERS = {
    'mpvoice': '[Audio]',
    'mp-common-mpaudio': '[Audio]',
    'mpgongyi': '[Charity]',
    'qqmusic': '[Music]',
    'mpshop': '[Shop]',
    'mp-weapp': '[Mini Program]',
    'mp-miniprogram': '[Mini Program]',
    'mpproduct': '[Product]',
    'mpcps': '[Product]',
}
_SHARE_TYPE_LABELS = {
    5: 'Video',
    6: 'Music',
    7: 'Audio',
    17: 'Short Post',
}

_JS_BOOTSTRAP = r"""
var globalThis = this;
var window = globalThis;
var self = globalThis;
var global = globalThis;
var console = {
  log: function(){},
  warn: function(){},
  error: function(){},
  info: function(){},
  debug: function(){}
};
var navigator = {
  userAgent: 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) MicroMessenger/8.0.0 Safari/537.36',
  platform: 'Linux x86_64',
  language: 'zh-CN',
  languages: ['zh-CN', 'zh']
};
var location = {
  href: 'https://mp.weixin.qq.com/s/test?__biz=fake&mid=1&idx=1',
  search: '?__biz=fake&mid=1&idx=1',
  hash: '',
  protocol: 'https:',
  host: 'mp.weixin.qq.com',
  hostname: 'mp.weixin.qq.com',
  origin: 'https://mp.weixin.qq.com',
  pathname: '/s/test'
};
var history = { replaceState: function(){}, pushState: function(){} };
var screen = { width: 1280, height: 800 };
var innerWidth = 1280;
var innerHeight = 800;
var devicePixelRatio = 1;

function fakeElement() {
  return {
    style: {},
    classList: {
      add: function(){},
      remove: function(){},
      contains: function(){ return false; }
    },
    dataset: {},
    childNodes: [],
    children: [],
    firstChild: null,
    parentNode: null,
    innerHTML: '',
    innerText: '',
    textContent: '',
    value: '',
    src: '',
    href: '',
    id: '',
    clientWidth: 667,
    clientHeight: 0,
    offsetWidth: 667,
    offsetHeight: 0,
    setAttribute: function(){},
    getAttribute: function(){ return ''; },
    removeAttribute: function(){},
    appendChild: function(){ return null; },
    removeChild: function(){ return null; },
    replaceChild: function(){ return null; },
    replaceWith: function(){},
    insertBefore: function(){ return null; },
    addEventListener: function(){},
    removeEventListener: function(){},
    dispatchEvent: function(){ return false; },
    querySelector: function(){ return null; },
    querySelectorAll: function(){ return []; },
    getElementsByTagName: function(){ return []; },
    getElementsByClassName: function(){ return []; },
    cloneNode: function(){ return fakeElement(); },
    getBoundingClientRect: function(){
      return { width: 0, height: 0, top: 0, left: 0, right: 0, bottom: 0 };
    },
    closest: function(){ return null; }
  };
}

var document = {
  body: fakeElement(),
  head: fakeElement(),
  documentElement: { clientWidth: 1280, clientHeight: 800, style: {} },
  readyState: 'complete',
  referrer: '',
  createElement: function(){ return fakeElement(); },
  createTextNode: function(){ return fakeElement(); },
  getElementById: function(){ return fakeElement(); },
  querySelector: function(){ return null; },
  querySelectorAll: function(){ return []; },
  getElementsByTagName: function(){ return []; },
  getElementsByClassName: function(){ return []; },
  addEventListener: function(){},
  removeEventListener: function(){}
};

var localStorage = {
  getItem: function(){ return null; },
  setItem: function(){},
  removeItem: function(){},
  clear: function(){}
};
var sessionStorage = {
  getItem: function(){ return null; },
  setItem: function(){},
  removeItem: function(){},
  clear: function(){}
};

function setTimeout(){ return 0; }
function clearTimeout(){}
function setInterval(){ return 0; }
function clearInterval(){}
function requestAnimationFrame(){ return 0; }
function cancelAnimationFrame(){}
function Image(){ return fakeElement(); }

window.window = window;
window.document = document;
window.navigator = navigator;
window.location = location;
window.history = history;
window.screen = screen;
window.localStorage = localStorage;
window.sessionStorage = sessionStorage;
window.setTimeout = setTimeout;
window.clearTimeout = clearTimeout;
window.setInterval = setInterval;
window.clearInterval = clearInterval;
window.requestAnimationFrame = requestAnimationFrame;
window.cancelAnimationFrame = cancelAnimationFrame;
window.Image = Image;
"""


@dataclass(slots=True)
class ParsedWechatArticle:
    title: str
    clean_html: str
    markdown: str
    item_show_type: int | None
    cgi_data: dict[str, Any]


def parse_wechat_article(
    raw_html: str,
    *,
    article_url: str | None = None,
    fallback_title: str | None = None,
) -> ParsedWechatArticle:
    cgi_data = extract_cgi_data(raw_html, article_url=article_url)
    item_show_type = _normalize_int(cgi_data.get('item_show_type'))
    title = _extract_title(cgi_data, fallback_title=fallback_title)
    body_html = _render_body_html(cgi_data, item_show_type=item_show_type, article_url=article_url)
    article_html = _build_article_fragment(title=title, body_html=body_html)
    clean_html = _build_document(
        title=title,
        item_show_type=item_show_type,
        article_html=article_html,
    )
    markdown = _postprocess_markdown(markdownify(article_html, heading_style='ATX'))
    return ParsedWechatArticle(
        title=title,
        clean_html=clean_html,
        markdown=markdown,
        item_show_type=item_show_type,
        cgi_data=cgi_data,
    )


def extract_cgi_data(raw_html: str, *, article_url: str | None = None) -> dict[str, Any]:
    soup = BeautifulSoup(raw_html, 'html.parser')
    scripts = soup.find_all('script')

    # Non-article pages (captcha, privacy-restricted, etc.) won't contain
    # cgiDataNew at all — skip JS evaluation and fail fast without retry.
    if 'cgiDataNew' not in raw_html:
        from .http import ArticleContentUnavailableError

        raise ArticleContentUnavailableError('Article content not available (page does not contain cgiDataNew)')

    ctx = quickjs.Context()
    ctx.set_time_limit(_JS_EVAL_TIMEOUT_MS)
    ctx.eval(_JS_BOOTSTRAP)
    if article_url:
        _set_location(ctx, article_url)

    for script in scripts:
        # Skip ESM scripts — QuickJS cannot execute type="module"
        if script.get('type') == 'module':
            continue
        code = script.string or script.text or ''
        if not code.strip():
            continue
        if any(marker in code for marker in _SKIPPED_SCRIPT_MARKERS):
            continue
        try:
            ctx.eval(f'try {{\n{code}\n}} catch (e) {{ window.__last_error__ = String(e); }}')
        except quickjs.JSException as exc:
            logger.debug('QuickJS skipped script due to exception: %s', exc)
        if ctx.eval('typeof window.cgiDataNew !== "undefined" && window.cgiDataNew ? 1 : 0'):
            payload = ctx.eval('JSON.stringify(window.cgiDataNew)')
            if not payload:
                break
            data = json.loads(payload)
            if isinstance(data, dict):
                return data
            break

    last_error = None
    try:
        last_error = ctx.eval('window.__last_error__ || null')
    except quickjs.JSException:
        last_error = None
    raise ValueError(f'Failed to extract cgiDataNew from article HTML. last_error={last_error!r}')


def _set_location(ctx: quickjs.Context, article_url: str) -> None:
    parsed = urlparse(article_url)
    href = json.dumps(article_url)
    search = json.dumps((parsed.query and f'?{parsed.query}') or '')
    hash_value = json.dumps((parsed.fragment and f'#{parsed.fragment}') or '')
    protocol = json.dumps(f'{parsed.scheme}:')
    host = json.dumps(parsed.netloc)
    hostname = json.dumps(parsed.hostname or '')
    origin = json.dumps(f'{parsed.scheme}://{parsed.netloc}')
    pathname = json.dumps(parsed.path or '/')
    ctx.eval(
        '\n'.join(
            [
                f'location.href = {href};',
                f'location.search = {search};',
                f'location.hash = {hash_value};',
                f'location.protocol = {protocol};',
                f'location.host = {host};',
                f'location.hostname = {hostname};',
                f'location.origin = {origin};',
                f'location.pathname = {pathname};',
            ]
        )
    )


def _extract_title(cgi_data: dict[str, Any], *, fallback_title: str | None) -> str:
    item_show_type = _normalize_int(cgi_data.get('item_show_type'))
    title = html.unescape(str(cgi_data.get('title') or '').strip())
    if item_show_type == 10:
        text_page_info = cgi_data.get('text_page_info') or {}
        if _normalize_int(text_page_info.get('is_user_title')) == 1 and title:
            return title
        if fallback_title:
            return html.unescape(fallback_title.strip()) or _TITLE_FALLBACK
        return _TITLE_FALLBACK
    if title:
        return title
    derived_title = _derive_title_from_type(cgi_data, item_show_type=item_show_type)
    if derived_title:
        return html.unescape(derived_title)
    if fallback_title:
        stripped = html.unescape(fallback_title.strip())
        if stripped:
            return stripped
    return _TITLE_FALLBACK


def _render_body_html(
    cgi_data: dict[str, Any],
    *,
    item_show_type: int | None,
    article_url: str | None,
) -> str:
    if item_show_type in (0, 11):
        fragment = _normalize_content_fragment(str(cgi_data.get('content_noencode') or ''), article_url=article_url)
        return f'<section class="wechat-content wechat-content-article">{fragment}</section>'
    if item_show_type == 8:
        return _render_picture_share_body(cgi_data)
    if item_show_type == 10:
        return _render_text_share_body(cgi_data)
    if item_show_type == 5:
        return _render_video_share_body(cgi_data)
    if item_show_type == 6:
        return _render_music_share_body(cgi_data)
    if item_show_type == 7:
        return _render_audio_share_body(cgi_data)
    if item_show_type == 17:
        return _render_short_share_body(cgi_data, article_url=article_url)
    fragment = _normalize_content_fragment(str(cgi_data.get('content_noencode') or ''), article_url=article_url)
    if fragment.strip():
        return f'<section class="wechat-content wechat-content-unknown">{fragment}</section>'
    return '<section class="wechat-content wechat-content-unknown"><p>[Unsupported Content]</p></section>'


def _render_picture_share_body(cgi_data: dict[str, Any]) -> str:
    description_html = _format_textish_html(str(cgi_data.get('content_noencode') or ''))
    description_soup = BeautifulSoup(description_html, 'html.parser')
    for link in description_soup.select('a.wx_img_refer_link'):
        seq = _normalize_int(link.get('data-seq'))
        if seq and not link.get('href'):
            link['href'] = f'#figure-{seq}'

    picture_items = cgi_data.get('picture_page_info_list') or []
    picture_html_parts: list[str] = []
    for index, item in enumerate(picture_items, start=1):
        if not isinstance(item, dict):
            continue
        cdn_url = str(item.get('cdn_url') or '').replace('&amp;', '&').strip()
        if not cdn_url:
            continue
        alt = html.escape(f'Figure {index}', quote=True)
        figure_id = f'figure-{index}'
        picture_html_parts.append(
            '\n'.join(
                [
                    f'<figure class="wechat-picture-item" id="{figure_id}">',
                    f'  <img src="{html.escape(cdn_url, quote=True)}" alt="{alt}">',
                    f'  <figcaption>Figure {index}</figcaption>',
                    '</figure>',
                ]
            )
        )
    return (
        '<section class="wechat-content wechat-content-picture">'
        f'<div class="wechat-picture-description">{description_soup.decode()}</div>'
        f'<div class="wechat-picture-gallery">{"".join(picture_html_parts)}</div>'
        '</section>'
    )


def _render_text_share_body(cgi_data: dict[str, Any]) -> str:
    text_page_info = cgi_data.get('text_page_info') or {}
    text_content = str(text_page_info.get('content_noencode') or cgi_data.get('content_noencode') or '')
    return (
        f'<section class="wechat-content wechat-content-text"><p>{_format_plain_text_html(text_content)}</p></section>'
    )


def _render_video_share_body(cgi_data: dict[str, Any]) -> str:
    description = str(cgi_data.get('content_noencode') or cgi_data.get('digest') or '').strip()
    video_page_infos = cgi_data.get('video_page_infos') or []
    cover_html = ''
    if isinstance(video_page_infos, list):
        for item in video_page_infos:
            if not isinstance(item, dict):
                continue
            cover_url = str(item.get('cover_url') or '').replace('&amp;', '&').strip()
            if cover_url:
                cover_html = (
                    '<figure class="wechat-video-cover">'
                    f'<img src="{html.escape(cover_url, quote=True)}" alt="Video cover">'
                    '<figcaption>[Video]</figcaption>'
                    '</figure>'
                )
                break
    description_html = f'<p>{_format_plain_text_html(description)}</p>' if description else ''
    return (
        '<section class="wechat-content wechat-content-video">'
        f'{cover_html}'
        f'{description_html or "<p>[Video]</p>"}'
        '</section>'
    )


def _render_music_share_body(cgi_data: dict[str, Any]) -> str:
    payload = _extract_music_share_payload(cgi_data)
    return _render_media_share_body(
        label=_SHARE_TYPE_LABELS[6],
        css_modifier='music',
        title=payload['title'] or '[Music]',
        cover_url=payload['cover_url'],
        meta_parts=[payload['artist'], payload['album'], payload['source']],
        description=payload['description'],
        link_text=payload['link_text'],
    )


def _render_audio_share_body(cgi_data: dict[str, Any]) -> str:
    rich_body = _extract_audio_rich_body_html(cgi_data)
    if rich_body:
        return f'<section class="wechat-content wechat-content-audio-article">{rich_body}</section>'
    payload = _extract_audio_share_payload(cgi_data)
    return _render_media_share_body(
        label=_SHARE_TYPE_LABELS[7],
        css_modifier='audio',
        title=payload['title'] or '[Audio]',
        cover_url=payload['cover_url'],
        meta_parts=[payload['creator'], payload['duration'], payload['album']],
        description=payload['description'],
        link_text=payload['link_text'],
    )


def _render_short_share_body(cgi_data: dict[str, Any], *, article_url: str | None) -> str:
    text_content = _pick_text(
        cgi_data,
        ('short_content',),
        ('content_noencode',),
        ('text_page_info', 'content_noencode'),
        ('digest',),
    )
    cover_url = _pick_url(
        cgi_data,
        ('cover_url',),
        ('cover',),
        ('share_img_url',),
        ('hd_head_img',),
    )
    source_text = _pick_text(cgi_data, ('short_link',), ('link',))
    if source_text and article_url and source_text == article_url:
        source_text = ''

    content_parts: list[str] = ['<section class="wechat-content wechat-content-short">']
    if cover_url:
        content_parts.append(
            '<figure class="wechat-short-cover">'
            f'<img src="{html.escape(cover_url, quote=True)}" alt="Short post cover">'
            '</figure>'
        )
    if text_content:
        content_parts.append(f'<p class="wechat-short-text">{_format_plain_text_html(text_content)}</p>')
    else:
        content_parts.append(f'<p class="wechat-short-text">[{html.escape(_SHARE_TYPE_LABELS[17])}]</p>')
    if source_text:
        content_parts.append(f'<p class="wechat-short-link">Source: {html.escape(source_text)}</p>')
    content_parts.append('</section>')
    return ''.join(content_parts)


def _normalize_content_fragment(content_html: str, *, article_url: str | None) -> str:
    if not content_html.strip():
        return ''
    soup = BeautifulSoup(content_html, 'html.parser')
    _normalize_fragment_dom(soup, article_url=article_url)
    return soup.decode()


def _normalize_fragment_dom(soup: BeautifulSoup, *, article_url: str | None) -> None:
    for script in soup.find_all('script'):
        script.decompose()
    for img in soup.find_all('img'):
        src = img.get('src') or img.get('data-src')
        if src:
            img['src'] = src
        if img.has_attr('data-src'):
            del img['data-src']
        if img.has_attr('height'):
            del img['height']
        img['loading'] = 'eager'
    for tag_name, placeholder in _EMBED_PLACEHOLDERS.items():
        for tag in soup.find_all(tag_name):
            replacement = soup.new_tag('p')
            replacement.string = placeholder
            tag.replace_with(replacement)
    for iframe in soup.find_all('iframe'):
        replacement = soup.new_tag('p')
        css_class = ' '.join(iframe.get('class') or [])
        if 'video_iframe' in css_class:
            replacement.string = '[Video]'
        elif 'js_editor_vote_card' in css_class:
            replacement.string = '[Vote]'
        else:
            replacement.string = '[Embedded Content]'
        iframe.replace_with(replacement)
    for anchor in soup.find_all('a'):
        href = anchor.get('href') or ''
        if not href and article_url:
            anchor['href'] = article_url
        elif href and article_url:
            anchor['href'] = urljoin(article_url, href)


def _build_article_fragment(*, title: str, body_html: str) -> str:
    escaped_title = html.escape(title)
    return (
        '<article class="wechat-article-root">\n'
        f'  <h1 class="wechat-article-title">{escaped_title}</h1>\n'
        f'  {body_html}\n'
        '</article>\n'
    )


def _build_document(*, title: str, item_show_type: int | None, article_html: str) -> str:
    body_class = f'wechat-article item-show-type-{item_show_type}' if item_show_type is not None else 'wechat-article'
    escaped_title = html.escape(title)
    return (
        '<!DOCTYPE html>\n'
        '<html lang="zh_CN">\n'
        '<head>\n'
        '  <meta charset="utf-8">\n'
        '  <meta http-equiv="X-UA-Compatible" content="IE=edge">\n'
        '  <meta name="viewport" content="width=device-width,initial-scale=1.0,maximum-scale=1.0,user-scalable=0,viewport-fit=cover">\n'
        '  <meta name="referrer" content="no-referrer">\n'
        f'  <title>{escaped_title}</title>\n'
        '  <style>\n'
        '    :root { color-scheme: light; --wechat-bg: #f7f8fa; --wechat-surface: #ffffff; --wechat-surface-muted: #f3f6fb; --wechat-text: #111827; --wechat-text-soft: #526071; --wechat-border: rgba(15, 23, 42, 0.08); --wechat-accent: #1d4ed8; }\n'
        '    body { margin: 0; font-family: "PingFang SC", "Noto Sans SC", system-ui, sans-serif; background: radial-gradient(circle at top, #ffffff 0%, var(--wechat-bg) 70%); color: var(--wechat-text); }\n'
        '    .wechat-article-root { max-width: 760px; margin: 0 auto; padding: 40px 20px 72px; }\n'
        '    .wechat-article-title { margin: 0 0 28px; font-size: clamp(32px, 5vw, 40px); line-height: 1.15; font-weight: 700; letter-spacing: -0.03em; }\n'
        '    .wechat-content { font-size: 17px; line-height: 1.85; }\n'
        '    .wechat-content img { max-width: 100%; height: auto; display: block; margin: 20px auto; }\n'
        '    .wechat-content figure { margin: 24px 0; }\n'
        '    .wechat-content figcaption { margin-top: 8px; color: var(--wechat-text-soft); font-size: 14px; text-align: center; }\n'
        '    .wechat-content a { color: var(--wechat-accent); text-decoration: none; }\n'
        '    .wechat-content pre { white-space: pre-wrap; word-break: break-word; }\n'
        '    .wechat-share-card { display: grid; gap: 18px; padding: 22px; border: 1px solid var(--wechat-border); border-radius: 24px; background: linear-gradient(180deg, var(--wechat-surface) 0%, var(--wechat-surface-muted) 100%); box-shadow: 0 18px 48px rgba(15, 23, 42, 0.08); }\n'
        '    .wechat-share-card-cover img { width: 100%; margin: 0; border-radius: 18px; aspect-ratio: 1 / 1; object-fit: cover; }\n'
        '    .wechat-share-card-kicker { margin: 0 0 8px; font-size: 12px; line-height: 1.2; font-weight: 700; letter-spacing: 0.12em; text-transform: uppercase; color: var(--wechat-text-soft); }\n'
        '    .wechat-share-card-title { margin: 0; font-size: 28px; line-height: 1.2; font-weight: 700; }\n'
        '    .wechat-share-card-meta { margin: 10px 0 0; color: var(--wechat-text-soft); font-size: 14px; line-height: 1.6; }\n'
        '    .wechat-share-card-summary, .wechat-share-card-link, .wechat-short-text, .wechat-short-link { margin: 16px 0 0; }\n'
        '    .wechat-short-cover img { margin: 0 0 16px; border-radius: 24px; }\n'
        '    .wechat-short-text { padding: 22px; border-radius: 24px; background: var(--wechat-surface); border: 1px solid var(--wechat-border); box-shadow: 0 12px 32px rgba(15, 23, 42, 0.06); }\n'
        '    @media (min-width: 680px) { .wechat-share-card { grid-template-columns: minmax(0, 220px) minmax(0, 1fr); align-items: center; } }\n'
        '  </style>\n'
        '</head>\n'
        f'<body class="{body_class}">\n'
        f'  {article_html}'
        '</body>\n'
        '</html>\n'
    )


def _format_textish_html(value: str) -> str:
    text = value.replace('\r\n', '\n').replace('\r', '\n')
    text = text.replace('\n', '<br>')
    return text


def _format_plain_text_html(value: str) -> str:
    escaped = html.escape(value.replace('\r\n', '\n').replace('\r', '\n'))
    return escaped.replace('\n', '<br>')


def _normalize_int(value: Any) -> int | None:
    if value in (None, ''):
        return None
    try:
        return int(value)
    except TypeError, ValueError:
        return None


def _postprocess_markdown(markdown: str) -> str:
    markdown = re.sub(r'(!\[[^\]]*\]\([^)]+\))', r'\n\1\n', markdown)
    lines = [line.strip() for line in markdown.splitlines()]
    empty_image_pattern = re.compile(r'^!\[[^\]]*]\(\s*\)$')
    js_void_pattern = re.compile(r'\]\(\s*javascript:void\(0\);?\s*\)', re.IGNORECASE)

    cleaned: list[str] = []
    for line in lines:
        if not line:
            cleaned.append('')
            continue
        if empty_image_pattern.match(line):
            continue
        cleaned.append(line)

    def is_plain_text_line(text: str) -> bool:
        if not text:
            return False
        if text.startswith(('#', '-', '*', '+', '>', '|', '![')):
            return False
        return not re.search(r'\[[^\]]+]\([^)]*\)', text)

    to_remove: set[int] = set()
    non_empty = [i for i, line in enumerate(cleaned) if line]
    js_void_lines = [i for i, line in enumerate(cleaned) if js_void_pattern.search(line)]
    for idx in js_void_lines:
        to_remove.add(idx)
        if idx not in non_empty:
            continue
        ordinal = non_empty.index(idx) + 1
        if ordinal > 8:
            continue
        prev_idx = idx - 1
        while prev_idx >= 0 and not cleaned[prev_idx]:
            prev_idx -= 1
        if prev_idx >= 0 and is_plain_text_line(cleaned[prev_idx]):
            to_remove.add(prev_idx)

    final_lines = [line for i, line in enumerate(cleaned) if i not in to_remove]
    return re.sub(r'\n{3,}', '\n\n', '\n'.join(final_lines)).strip()


def _derive_title_from_type(cgi_data: dict[str, Any], *, item_show_type: int | None) -> str:
    if item_show_type == 6:
        return _extract_music_share_payload(cgi_data)['title']
    if item_show_type == 7:
        return _extract_audio_share_payload(cgi_data)['title']
    if item_show_type == 17:
        content = _pick_text(
            cgi_data,
            ('short_content',),
            ('content_noencode',),
            ('text_page_info', 'content_noencode'),
        )
        if content:
            first_line = content.splitlines()[0].strip()
            return first_line[:60]
    return ''


def _extract_music_share_payload(cgi_data: dict[str, Any]) -> dict[str, str]:
    candidate = _select_best_mapping(
        _collect_candidate_mappings(
            cgi_data,
            'music_page_info',
            'qqmusic_info',
            'finder_music_card',
            'finder_music_card_list',
        ),
        score_paths=(
            ('song_name',),
            ('music_name',),
            ('title',),
            ('name',),
            ('singer',),
            ('artist',),
            ('cover_url',),
            ('cover',),
        ),
    )
    description = _pick_text(cgi_data, ('content_noencode',), ('digest',))
    return {
        'title': _pick_text(candidate, ('song_name',), ('music_name',), ('title',), ('name',))
        or _pick_text(cgi_data, ('title',)),
        'artist': _pick_text(candidate, ('singer',), ('artist',), ('nickname',), ('author',)),
        'album': _pick_text(candidate, ('album_name',), ('source',), ('music_source',)),
        'source': _pick_text(candidate, ('source_name',), ('source',)),
        'cover_url': _pick_url(candidate, ('cover_url',), ('cover',), ('img_url',), ('hd_cover_img',), ('pic_url',)),
        'description': description,
        'link_text': _pick_text(candidate, ('url',), ('play_url',), ('music_url',), ('link',))
        or _pick_text(cgi_data, ('short_link',), ('link',)),
    }


def _extract_audio_share_payload(cgi_data: dict[str, Any]) -> dict[str, str]:
    candidate = _select_best_mapping(
        _collect_candidate_mappings(
            cgi_data,
            'audio_page_info',
            'audio_info',
            'voice_info',
            'voice_in_appmsg',
            'voice_in_appmsg_list_json',
            'finder_audio_card',
            'finder_audio_card_list',
        ),
        score_paths=(
            ('title',),
            ('name',),
            ('voice_name',),
            ('audio_name',),
            ('nickname',),
            ('author',),
            ('duration',),
            ('play_length',),
            ('cover_url',),
            ('cover',),
        ),
    )
    description = _pick_text(cgi_data, ('content_noencode',), ('digest',))
    return {
        'title': _pick_text(candidate, ('title',), ('name',), ('voice_name',), ('audio_name',))
        or _pick_text(cgi_data, ('title',)),
        'creator': _pick_text(candidate, ('nickname',), ('author',), ('nick_name',), ('biz_nickname',)),
        'album': _pick_text(candidate, ('appmsgalbuminfo', 'title'), ('album_name',), ('topic_name',)),
        'duration': _format_duration(
            _pick_text(
                candidate,
                ('play_length',),
                ('duration',),
                ('duration_ms',),
                ('time',),
            )
            or _pick_text(cgi_data, ('media_duration',))
        ),
        'cover_url': _pick_url(candidate, ('cover_url',), ('cover',), ('img_url',), ('hd_cover_img',), ('pic_url',)),
        'description': description,
        'link_text': _pick_text(candidate, ('url',), ('play_url',), ('link',))
        or _pick_text(cgi_data, ('short_link',), ('link',)),
    }


def _render_media_share_body(
    *,
    label: str,
    css_modifier: str,
    title: str,
    cover_url: str,
    meta_parts: list[str],
    description: str,
    link_text: str,
) -> str:
    content_parts: list[str] = [f'<section class="wechat-content wechat-content-{css_modifier}">']
    content_parts.append(f'<div class="wechat-share-card wechat-share-card-{css_modifier}">')
    if cover_url:
        content_parts.append(
            '<figure class="wechat-share-card-cover">'
            f'<img src="{html.escape(cover_url, quote=True)}" alt="{html.escape(title or label, quote=True)} cover">'
            '</figure>'
        )
    content_parts.append('<div class="wechat-share-card-body">')
    content_parts.append(f'<p class="wechat-share-card-kicker">{html.escape(label)}</p>')
    content_parts.append(f'<h2 class="wechat-share-card-title">{html.escape(title)}</h2>')
    normalized_meta = [part for part in meta_parts if part]
    if normalized_meta:
        content_parts.append(f'<p class="wechat-share-card-meta">{html.escape(" · ".join(normalized_meta))}</p>')
    if description:
        content_parts.append(f'<p class="wechat-share-card-summary">{_format_plain_text_html(description)}</p>')
    if link_text:
        content_parts.append(f'<p class="wechat-share-card-link">Source: {html.escape(link_text)}</p>')
    content_parts.append('</div></div></section>')
    return ''.join(content_parts)


def _collect_candidate_mappings(cgi_data: dict[str, Any], *root_keys: str) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for key in root_keys:
        value = _coerce_json_like(cgi_data.get(key))
        candidates.extend(_flatten_candidate_value(value))
    return candidates


def _flatten_candidate_value(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, dict):
        items = [value]
        for list_key in ('list', 'voice_in_appmsg', 'audio_infos'):
            nested = value.get(list_key)
            if isinstance(nested, list):
                items.extend(item for item in nested if isinstance(item, dict))
        return items
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return []


def _extract_audio_rich_body_html(cgi_data: dict[str, Any]) -> str:
    for key in ('content_noencode', 'digest'):
        raw = str(cgi_data.get(key) or '').strip()
        if not raw:
            continue
        if 'wx_audio_timepoint_tag' not in raw and '<' not in raw:
            continue
        return _normalize_content_fragment(raw, article_url=_pick_text(cgi_data, ('link',)))
    return ''


def _select_best_mapping(
    candidates: list[dict[str, Any]], *, score_paths: tuple[tuple[str, ...], ...]
) -> dict[str, Any]:
    best_candidate: dict[str, Any] = {}
    best_score = -1
    for candidate in candidates:
        score = sum(1 for path in score_paths if _pick_text(candidate, path))
        if score > best_score:
            best_candidate = candidate
            best_score = score
    return best_candidate


def _coerce_json_like(value: Any) -> Any:
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith('{') or stripped.startswith('['):
            try:
                return json.loads(stripped)
            except json.JSONDecodeError:
                return value
    return value


def _pick_text(source: Any, *paths: tuple[str, ...]) -> str:
    for path in paths:
        value = _extract_path(source, path)
        if value in (None, ''):
            continue
        if isinstance(value, (int, float)):
            return str(value)
        if isinstance(value, str):
            text = value.strip()
            if text:
                return text
    return ''


def _pick_url(source: Any, *paths: tuple[str, ...]) -> str:
    value = _pick_text(source, *paths)
    return value.replace('&amp;', '&') if value else ''


def _extract_path(source: Any, path: tuple[str, ...]) -> Any:
    current = source
    for segment in path:
        current = _coerce_json_like(current)
        if not isinstance(current, dict):
            return None
        current = current.get(segment)
    return _coerce_json_like(current)


def _format_duration(value: str) -> str:
    text = value.strip()
    if not text:
        return ''
    if ':' in text:
        return text
    try:
        numeric = int(text)
    except ValueError:
        return text
    if numeric > 10_000:
        numeric //= 1000
    hours, remainder = divmod(max(numeric, 0), 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f'{hours:d}:{minutes:02d}:{seconds:02d}'
    return f'{minutes:d}:{seconds:02d}'


__all__ = ['ParsedWechatArticle', 'extract_cgi_data', 'parse_wechat_article']
