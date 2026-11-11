"""Checking a submitted credential before it is trusted.

A cookie goes straight into an HTTP header (Bilibili) or a file ``yt-dlp`` reads
(YouTube), so the value is checked for shape rather than taken on faith: a
truncated paste of a settings page or a JSON blob would otherwise be sent as a
header or written to disk as if it were a cookie file. The rules are shared by the
per-user endpoint and the instance-wide console, which must not disagree.
"""

from __future__ import annotations

import re

#: Ceiling on a stored credential. A real cookies.txt for Google is ~10 KB.
MAX_CREDENTIAL_BYTES = 64 * 1024

#: `name=value`, optionally repeated with `; ` between pairs.
_COOKIE_HEADER = re.compile(r'[A-Za-z0-9_\-.]+=[^;\s]*(?:;\s*[A-Za-z0-9_\-.]+=[^;\s]*)*;?')

#: A Netscape cookies.txt line: domain, flag, path, secure, expiry, name, value.
_NETSCAPE_LINE = re.compile(r'^[^\t]+\t(?:TRUE|FALSE)\t[^\t]*\t(?:TRUE|FALSE)\t\d+\t[^\t]+\t[^\t]*$')

SOURCES = ('bilibili', 'youtube', 'caixin')


class CredentialError(ValueError):
    """The submitted value is not a credential of that kind."""


def clean(source: str, secret: object) -> str:
    """Normalise ``secret`` for ``source``; an empty value means "clear it".

    Raises ``CredentialError`` when the value cannot be what it claims to be.
    """
    text = str(secret or '').strip()
    if source not in SOURCES:
        raise CredentialError(f'不支持为 {source} 配置凭证')
    if not text:
        return ''
    if len(text.encode('utf-8')) > MAX_CREDENTIAL_BYTES:
        raise CredentialError(f'凭证过大，上限 {MAX_CREDENTIAL_BYTES // 1024} KiB')

    if source in {'bilibili', 'caixin'}:
        # 一行 Cookie 头：换行会被 HTTP 客户端当成头注入拒绝，先压平。
        cookie = ' '.join(text.split())
        if not _COOKIE_HEADER.fullmatch(cookie):
            message = '请输入 name=value; name=value 格式的 Cookie' if source == 'caixin' else '这不是 Cookie 的样子（形如 SESSDATA=…; bili_jct=…）'
            raise CredentialError(message)
        if source == 'caixin':
            if any(ord(char) < 0x20 or ord(char) > 0x7E for char in cookie):
                raise CredentialError('财新 Cookie 中含有无效字符')
            pairs = [part.strip().split('=', 1) for part in cookie.rstrip(';').split(';')]
            values = dict(pairs)
            if len(values) != len(pairs):
                raise CredentialError('Cookie 中有重名字段，请重新导出财新凭证')
            uid = values.get('UID', '')
            if not uid.isascii() or not uid.isdecimal() or len(uid) > 19 or not 0 < int(uid) <= 2**63 - 1:
                raise CredentialError('财新 Cookie 需要有效的 UID')
            if not values.get('SA_USER_auth'):
                raise CredentialError('财新 Cookie 中没有 SA_USER_auth，请先登录财新')
            return cookie
        if 'SESSDATA=' not in cookie:
            raise CredentialError('Cookie 里没有 SESSDATA，B站接口不接受匿名请求')
        return cookie

    lines = [line for line in text.splitlines() if line.strip() and not line.lstrip().startswith('#')]
    if not any(_NETSCAPE_LINE.match(line) for line in lines):
        raise CredentialError('这不是 Netscape 格式的 cookies.txt（yt-dlp --cookies 需要它）')
    return text


__all__ = ['MAX_CREDENTIAL_BYTES', 'SOURCES', 'CredentialError', 'clean']
