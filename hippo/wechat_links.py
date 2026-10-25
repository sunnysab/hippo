"""Recognize article identities without requesting a WeChat URL."""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlsplit, urlunsplit

_URL = re.compile(r'(?:https?:)?//mp\.weixin\.qq\.com[^\s<>"\'`)\]]*', re.IGNORECASE)
_TOKEN = re.compile(r'[A-Za-z0-9_-]{1,128}')
_BIZ = re.compile(r'[A-Za-z0-9_+/=-]{1,256}')


@dataclass(frozen=True, slots=True)
class WechatLink:
    url: str
    identity: str
    biz: str = ''
    sn: str = ''
    slug: str = ''
    mid: str = ''
    idx: str = ''

    @property
    def queue_sn(self) -> str:
        return self.sn or self.identity


def parse_wechat_link(value: str) -> WechatLink | None:
    value = html.unescape(value.strip())
    if value.startswith('//'):
        value = f'https:{value}'
    if len(value) > 8192 or any(ord(c) < 33 or c == '\\' for c in value):
        return None
    try:
        url = urlsplit(value)
        if (
            url.scheme not in {'http', 'https'}
            or url.hostname != 'mp.weixin.qq.com'
            or url.username is not None
            or url.password is not None
            or url.port not in {None, 80, 443}
        ):
            return None
        normalized = urlunsplit(('https', 'mp.weixin.qq.com', url.path, url.query, url.fragment))
        if url.path.startswith('/s/'):
            slug = url.path.removeprefix('/s/')
            if _TOKEN.fullmatch(slug):
                return WechatLink(normalized, f'slug:{slug}', slug=slug)
            return None
        if url.path not in {'/s', '/mp/appmsg/show'}:
            return None
        query = parse_qs(url.query)
        # Ambiguous identity parameters must never resolve to somebody else's article.
        if any(len(query.get(key, [])) > 1 for key in ('__biz', 'sn', 'mid', 'idx', 'appmsgid', 'itemidx')):
            return None

        def first(key: str) -> str:
            return next(iter(query.get(key, [])), '')

        biz, sn = first('__biz'), first('sn')
        mid, idx = first('mid') or first('appmsgid'), first('idx') or first('itemidx')
        if not _BIZ.fullmatch(biz):
            return None
        if sn and _TOKEN.fullmatch(sn):
            return WechatLink(normalized, f'sn:{biz}:{sn}', biz=biz, sn=sn, mid=mid, idx=idx)
        if re.fullmatch(r'[0-9]{1,20}', mid) and re.fullmatch(r'[1-9][0-9]{0,4}', idx):
            return WechatLink(normalized, f'mid:{biz}:{mid}:{idx}', biz=biz, mid=mid, idx=idx)
    except ValueError:
        return None
    return None


def extract_wechat_links(blocks: list[dict[str, Any]]) -> list[WechatLink]:
    """Keep source URLs and order; skip code and deduplicate repeated URLs."""
    links: dict[str, WechatLink] = {}
    for block in blocks:
        if not isinstance(block, dict) or block.get('type') == 'code':
            continue
        href = block.get('href')
        candidates = [href] if isinstance(href, str) else []
        text = block.get('text')
        if isinstance(text, str):
            text = re.sub(r'(`+).*?\1', '', text, flags=re.DOTALL)
            candidates.extend(match.group().rstrip('*,.;:!?，。；：！？、》」』') for match in _URL.finditer(text))
        for candidate in candidates:
            if link := parse_wechat_link(candidate):
                links.setdefault(link.url, link)
    return list(links.values())
