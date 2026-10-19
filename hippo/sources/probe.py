"""Asking a source whether the credential hippo holds actually works.

An expired cookie looks exactly like a working one until something fails on it, and
that failure is easy to misread: Bilibili answers 412 for "no session" as readily as
for "too many requests", and YouTube quietly serves the bot check instead of the
video. One cheap call per source answers the question directly — and the answer
names the account it got in as, so "it works" is something you can see.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..logger import get_logger
from .bilibili import BilibiliClient, BilibiliError
from .youtube import YoutubeError, is_bot_check, parse_json_lines, run_ytdlp

logger = get_logger(__name__)

#: A logged-in-only page. Fetching a video says nothing about the cookie — YouTube
#: answers anonymous callers happily until it decides they are a bot, so an invalid
#: cookie looks the same as a valid one. The subscription feed does not: measured on
#: the real proxies, an anonymous session gets an *empty* list with exit code 0,
#: while a live login gets its subscriptions.
YOUTUBE_PROBE_URL = 'https://www.youtube.com/feed/subscriptions'
PROBE_TIMEOUT = 90.0

@dataclass(frozen=True, slots=True)
class CredentialProbe:
    """What one call to the source said about the credential it was given."""

    source: str
    ok: bool
    detail: str
    #: What the credential turned out to be good for: the account name, or the video
    #: that could be described. Never the credential itself.
    account: str = ''


async def probe_credential(*, source: str, cookie: str, proxy: str = '') -> CredentialProbe:
    """Ask ``source`` what it makes of ``cookie``. Never raises."""
    if source == 'bilibili':
        return await _probe_bilibili(cookie=cookie, proxy=proxy)
    if source == 'youtube':
        return await _probe_youtube(cookie=cookie, proxy=proxy)
    return CredentialProbe(source=source, ok=False, detail=f'还不知道怎么测 {source!r} 的凭证')


async def _probe_bilibili(*, cookie: str, proxy: str) -> CredentialProbe:
    client = BilibiliClient(cookie=cookie, proxy=proxy)
    try:
        payload = await client.nav()
    except BilibiliError as exc:
        return CredentialProbe(source='bilibili', ok=False, detail=str(exc))
    except Exception as exc:  # 探测不该把一个管理页拖垮
        # 只留类型名：异常文本可能带上请求上下文（httpx 的报错里有 URL），而这条
        # 既会进日志也会进 HTTP 响应——Cookie 不能跟着走。
        logger.warning('B站凭证探测出现未预期的异常：%s', type(exc).__name__)
        return CredentialProbe(source='bilibili', ok=False, detail=f'探测失败：{type(exc).__name__}')
    finally:
        await client.aclose()

    data: dict[str, Any] = payload.get('data') or {}
    if payload.get('code') == -101 or not data.get('isLogin'):
        return CredentialProbe(
            source='bilibili',
            ok=False,
            detail='B站认为当前是未登录状态：Cookie 缺失或已过期',
        )
    return CredentialProbe(
        source='bilibili',
        ok=True,
        detail='已登录，投稿列表接口不会再被风控挡下',
        account=str(data.get('uname') or data.get('mid') or ''),
    )


async def _probe_youtube(*, cookie: str, proxy: str) -> CredentialProbe:
    try:
        output = await run_ytdlp(
            [
                '--flat-playlist',
                '--dump-json',
                # 只问第一条：只要列表不是空的，登录态就已经被认了。
                '-I',
                '1:1',
                YOUTUBE_PROBE_URL,
            ],
            proxy=proxy,
            timeout=PROBE_TIMEOUT,
            cookies=cookie,
        )
    except YoutubeError as exc:
        detail = str(exc)
        if is_bot_check(detail):
            return CredentialProbe(
                source='youtube',
                ok=False,
                detail='YouTube 要求登录（bot 检查）：这个出口需要一份有效 Cookie',
            )
        return CredentialProbe(source='youtube', ok=False, detail=f'探测失败：{detail}')

    entries = parse_json_lines(output)
    if not entries:
        # 实测：未登录时 YouTube 不回错误，只给一个空列表。账号本身没有订阅时也是这样，
        # 所以把看到的东西原样说出来，别让它看起来像是「cookie 一定坏了」。
        return CredentialProbe(
            source='youtube',
            ok=False,
            detail='YouTube 没把这份登录态当回事（订阅列表是空的；账号确实没有订阅时也会如此）',
        )
    return CredentialProbe(
        source='youtube',
        ok=True,
        detail='登录态被认可：能读到你自己的订阅',
        account=str(entries[0].get('playlist_title') or entries[0].get('title') or '订阅列表'),
    )


__all__ = ['PROBE_TIMEOUT', 'YOUTUBE_PROBE_URL', 'CredentialProbe', 'probe_credential']
