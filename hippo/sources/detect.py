"""Which source does a pasted URL belong to?

The reader pastes one thing — a video, a channel, a feed. Deciding where it goes is
the whole job of this module, and it is deliberately the only place that knows the
hostnames: everything downstream dispatches on ``accounts.source``.
"""

from __future__ import annotations

from urllib.parse import urlparse

BILIBILI_HOSTS = {'www.bilibili.com', 'bilibili.com', 'm.bilibili.com', 'space.bilibili.com', 'b23.tv'}
WECHAT_HOSTS = {'mp.weixin.qq.com'}
YOUTUBE_HOSTS = {
    'www.youtube.com',
    'youtube.com',
    'm.youtube.com',
    'music.youtube.com',
    'youtu.be',
    'www.youtu.be',
}


def detect_source(value: str) -> str | None:
    """Return the source name for a URL, or ``None`` when it cannot be one.

    A WeChat article link is the one kind of URL that names an account rather
    than a feed: it carries the publisher, so the caller can subscribe to it
    without a search. Anything else that is http(s) is treated as a podcast
    feed — a feed has no recognisable hostname, so the only way to tell is to
    fetch it, which the caller does.
    """
    url = (value or '').strip()
    if not url:
        return None
    parsed = urlparse(url)
    if parsed.scheme not in {'http', 'https'} or not parsed.hostname:
        return None
    host = parsed.hostname.lower()
    if host in BILIBILI_HOSTS:
        return 'bilibili'
    if host in YOUTUBE_HOSTS:
        return 'youtube'
    if host in WECHAT_HOSTS:
        return 'weixin'
    return 'podcast'


__all__ = ['BILIBILI_HOSTS', 'WECHAT_HOSTS', 'YOUTUBE_HOSTS', 'detect_source']
