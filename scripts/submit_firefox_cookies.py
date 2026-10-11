#!/usr/bin/env python3
"""Submit this machine's Firefox cookies for Bilibili and YouTube to a hippo instance.

Both sources need a login to be useful: Bilibili's listing endpoint answers 412
without a session, and YouTube answers the bot check instead of metadata. Firefox
already holds those sessions, so this script reads them out and hands them over
over HTTP.

Privacy rules, which the whole script follows:

* A cookie value is never printed, never logged, and never included in an error
  message. The output is counts, cookie *names* and sizes, so a terminal log or a
  bug report stays safe to paste.
* The database is read in place, read-only. If SQLite refuses a lock (Firefox
  writing at that moment), a copy is taken into a private temporary directory and
  removed right after — never the original file, never anything left behind.
* ``--dry-run`` shows what would be sent without sending it.

Run it with any Python 3.11+; it uses the standard library only::

    python3 scripts/submit_firefox_cookies.py --server https://hippo.example.com

The default target is the *instance* credential, which is what the background sync
uses, so an admin gets working sources for everyone. ``--scope user`` writes your
own credential instead, which only applies to what you trigger by hand.
"""

from __future__ import annotations

import argparse
import configparser
import getpass
import json
import os
import shutil
import sqlite3
import ssl
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

#: Firefox profile roots, in the order they are tried.
PROFILE_ROOTS = (
    Path.home() / '.mozilla' / 'firefox',
    Path.home() / 'Library' / 'Application Support' / 'Firefox' / 'Profiles',
    Path(os.environ.get('APPDATA', Path.home())) / 'Mozilla' / 'Firefox' / 'Profiles',
)

#: Which cookies each source needs, and what a session is called there. ``expected``
#: is both what the output reports and what decides *which context* to read: a
#: browser can hold several cookies with one name, and only one of them belongs to
#: the session the person means.
SOURCES: dict[str, dict[str, Any]] = {
    'bilibili': {
        'domains': ('bilibili.com',),
        'expected': ('SESSDATA',),
        'label': 'B站',
    },
    'youtube': {
        'domains': ('youtube.com', 'google.com'),
        'expected': ('SID', 'HSID', 'SSID', 'SAPISID', '__Secure-1PSID', '__Secure-3PSID'),
        'label': 'YouTube',
    },
}

#: Firefox keeps several cookies with the same name apart in ``originAttributes``.
#: Two kinds are not the person's own session and must never be mixed in:
#:
#: * ``^partitionKey=(https,site)`` — a CHIPS copy stored for an embedded Google
#:   widget on *that* site. Every one of them is a different (and unrelated) session.
#: * ``^userContextId=N`` — a container tab. Choosing the right one is fine; mixing
#:   it with the default context is not.
#:
#: Getting this wrong is invisible: the header just carries two values for one name,
#: and the server picks one. Measured on a real Firefox profile, that produced 64
#: cookie pairs for 44 distinct names and YouTube answered ``LOGGED_IN: false``.
_PARTITION_MARKER = 'partitionKey'
_CONTAINER_MARKER = 'userContextId'

#: The session cookie hippo issues on login.
SESSION_COOKIE = 'hippo_session'

#: Where each source's *login* actually lives. This matters more than it looks: a
#: profile can hold a Google login and no YouTube login at all, and the two are not
#: interchangeable — a browser (and yt-dlp's cookiejar) never sends google.com
#: cookies to youtube.com. Measured against a real profile: 12 login cookies on
#: .google.com and zero on .youtube.com, so YouTube answered LOGGED_IN: false while
#: every name in the set looked present.
SESSION_HOST = {'bilibili': 'bilibili.com', 'youtube': 'youtube.com'}

#: A cookie value with a tab or newline would corrupt the Netscape file, and
#: Firefox cannot store one anyway. Skipped rather than escaped.
_FORBIDDEN_IN_VALUE = ('\t', '\n', '\r')


class ScriptError(RuntimeError):
    """Something the person running this can fix."""


# --------------------------------------------------------------------------- #
# Firefox
# --------------------------------------------------------------------------- #


def profiles() -> list[tuple[str, Path]]:
    """Every Firefox profile that has a cookie database, newest first."""
    found: list[tuple[str, Path]] = []
    for root in PROFILE_ROOTS:
        if not root.is_dir():
            continue
        for candidate in sorted(root.iterdir()):
            if (candidate / 'cookies.sqlite').is_file():
                found.append((candidate.name, candidate))
    return found


def default_profile() -> Path:
    """The profile Firefox would use: profiles.ini first, then a lone profile."""
    for root in PROFILE_ROOTS:
        ini = root / 'profiles.ini'
        if not ini.is_file():
            continue
        parser = configparser.ConfigParser()
        parser.read(ini, encoding='utf-8')
        default_name = ''
        for section in parser.sections():
            if section.startswith('Install') and parser.has_option(section, 'Default'):
                default_name = parser.get(section, 'Default')
                break
        for section in parser.sections():
            if not section.startswith('Profile'):
                continue
            name = parser.get(section, 'Name', fallback='')
            is_default = parser.getboolean(section, 'Default', fallback=False)
            path = parser.get(section, 'Path', fallback='')
            if not path:
                continue
            if name == default_name or (not default_name and is_default and section == 'Profile0'):
                resolved = (root / path).resolve()
                if (resolved / 'cookies.sqlite').is_file():
                    return resolved
    candidates = profiles()
    if not candidates:
        raise ScriptError('没找到 Firefox 的 cookies.sqlite，用 --profile 指定 profile 目录')
    if len(candidates) == 1:
        return candidates[0][1]
    names = ', '.join(name for name, _ in candidates)
    raise ScriptError(f'有多个 profile（{names}），用 --profile 指定其中一个')


_SELECT_COOKIES = (
    'SELECT host, name, value, path, expiry, isSecure, isHttpOnly, originAttributes FROM moz_cookies'
)


def _read(database: Path) -> list[dict[str, Any]]:
    connection = sqlite3.connect(f'file:{database}?mode=ro', uri=True, timeout=2.0)
    try:
        connection.row_factory = sqlite3.Row
        return [dict(row) for row in connection.execute(_SELECT_COOKIES).fetchall()]
    finally:
        connection.close()


def _copy_alongside(database: Path) -> tuple[Path, tempfile.TemporaryDirectory]:
    """Copy the database *and* its write-ahead log, so the copy reads consistently."""
    workspace = tempfile.TemporaryDirectory(prefix='hippo-firefox-cookies-')
    target = Path(workspace.name) / database.name
    for suffix in ('', '-wal', '-shm'):
        source = database.with_name(database.name + suffix) if suffix else database
        if source.is_file():
            shutil.copy2(source, target.with_name(target.name + suffix) if suffix else target)
    return target, workspace


def read_cookies(profile: Path) -> list[dict[str, Any]]:
    """Read the cookie table, whether or not Firefox is running.

    A live profile is read in place (read-only). SQLite can still refuse the lock
    while Firefox is mid-write, and its backup API would then wait forever, so the
    fallback is a copy — read within the copy, deleted immediately after.
    """
    database = profile / 'cookies.sqlite'
    if not database.is_file():
        raise ScriptError(f'{profile} 里没有 cookies.sqlite')
    try:
        return _read(database)
    except sqlite3.Error:
        pass
    try:
        copy, workspace = _copy_alongside(database)
        try:
            return _read(copy)
        finally:
            workspace.cleanup()
    except (sqlite3.Error, OSError) as exc:
        raise ScriptError(f'读取 cookie 表失败（Firefox 正占着它？）：{exc}') from exc


def _context(row: dict[str, Any]) -> str:
    return str(row.get('originAttributes') or '')


def _is_partitioned(context: str) -> bool:
    """A third-party (CHIPS) copy: never the session the person is thinking of."""
    return _PARTITION_MARKER in context


def usable_contexts(rows: list[dict[str, Any]], source: str) -> dict[str, list[dict[str, Any]]]:
    """Group ``rows`` by Firefox context, dropping partitions and dead rows."""
    domains = SOURCES[source]['domains']
    now = int(time.time())
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        host = str(row['host'] or '')
        bare = host.lstrip('.')
        if not any(bare == domain or bare.endswith(f'.{domain}') for domain in domains):
            continue
        if _is_partitioned(_context(row)):
            continue
        expiry = int(row['expiry'] or 0)
        if expiry and expiry < now:
            continue
        if any(char in str(row['value'] or '') for char in _FORBIDDEN_IN_VALUE):
            continue
        groups.setdefault(_context(row), []).append(row)
    return groups


def pick_context(rows: list[dict[str, Any]], source: str) -> str:
    """Which context holds the session: the default one, unless only a container does.

    A browser used normally keeps everything in the default context, and that is what
    is preferred even when a container also has a login. Falling back to a container
    covers the person who logged in *inside* one.
    """
    groups = usable_contexts(rows, source)
    if not groups:
        return ''
    expected = set(SOURCES[source]['expected'])

    def has_session(context: str) -> bool:
        return any(str(row['name']) in expected for row in groups[context])

    if '' in groups and has_session(''):
        return ''
    containers = [context for context in groups if _CONTAINER_MARKER in context and has_session(context)]
    if containers:
        return sorted(containers)[0]
    return '' if '' in groups else sorted(groups)[0]


def cookies_for(rows: list[dict[str, Any]], source: str) -> list[dict[str, Any]]:
    """The cookies a request to ``source`` would carry: one context, unexpired."""
    context = pick_context(rows, source)
    selected = usable_contexts(rows, source).get(context, [])
    # 同名只留一个：同一上下文的重复（不同 path）取路径更长的那个。
    by_name: dict[str, dict[str, Any]] = {}
    for row in sorted(selected, key=lambda r: len(str(r['path'] or ''))):
        by_name[str(row['name'])] = row
    return sorted(by_name.values(), key=lambda row: (str(row['host']), str(row['name'])))


def as_cookie_header(rows: list[dict[str, Any]]) -> str:
    """A single ``Cookie:`` value, which is what the Bilibili client sends."""
    return '; '.join(f'{row["name"]}={row["value"]}' for row in rows)


def as_netscape_file(rows: list[dict[str, Any]]) -> str:
    """A cookies.txt, which is the only thing ``yt-dlp --cookies`` accepts."""
    lines = ['# Netscape HTTP Cookie File', '# Written by hippo scripts/submit_firefox_cookies.py', '']
    for row in rows:
        host = str(row['host'] or '')
        include_subdomains = 'TRUE' if host.startswith('.') else 'FALSE'
        secure = 'TRUE' if row['isSecure'] else 'FALSE'
        expiry = int(row['expiry'] or 0)
        prefix = '#HttpOnly_' if row['isHttpOnly'] else ''
        lines.append(
            f'{prefix}{host}\t{include_subdomains}\t{row["path"] or "/"}\t{secure}\t{expiry}'
            f'\t{row["name"]}\t{row["value"]}'
        )
    return '\n'.join(lines) + '\n'


# --------------------------------------------------------------------------- #
# hippo
# --------------------------------------------------------------------------- #


class Session:
    """A logged-in connection to one hippo instance."""

    def __init__(self, server: str, *, insecure: bool = False) -> None:
        self.server = server.rstrip('/')
        self.cookie = ''
        self.ssl_context = ssl._create_unverified_context() if insecure else None

    def request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
        body = None if payload is None else json.dumps(payload).encode('utf-8')
        request = urllib.request.Request(f'{self.server}{path}', data=body, method=method)
        request.add_header('Accept', 'application/json')
        if body is not None:
            request.add_header('Content-Type', 'application/json')
        if self.cookie:
            request.add_header('Cookie', f'hippo_session={self.cookie}')
        try:
            with urllib.request.urlopen(request, timeout=30, context=self.ssl_context) as response:
                self._remember(response)
                raw = response.read().decode('utf-8', 'replace')
                return json.loads(raw) if raw.strip() else None
        except urllib.error.HTTPError as exc:
            raise ScriptError(f'{method} {path} 失败：HTTP {exc.code}{_detail(exc)}') from exc
        except urllib.error.URLError as exc:
            raise ScriptError(f'连不上 {self.server}：{exc.reason}') from exc

    def _remember(self, response: Any) -> None:
        """Keep the session cookie. Only the cookie name is ever looked at."""
        for header in response.headers.get_all('Set-Cookie') or []:
            if header.startswith(f'{SESSION_COOKIE}='):
                self.cookie = header.split(';', 1)[0].split('=', 1)[1]

    def login(self, username: str, password: str) -> str:
        """Sign in and return the account's role."""
        self.request('POST', '/api/auth/login', {'username': username, 'password': password})
        if not self.cookie:
            raise ScriptError('登录没有拿到会话 cookie（服务地址或反向代理配置要检查）')
        me = self.request('GET', '/api/auth/me') or {}
        return str(me.get('role') or me.get('user', {}).get('role') or '')


def _detail(exc: urllib.error.HTTPError) -> str:
    """The server's own message. It never contains the submitted value."""
    try:
        payload = json.loads(exc.read().decode('utf-8', 'replace'))
    except (ValueError, OSError):
        return ''
    for key in ('error', 'detail', 'message'):
        if isinstance(payload, dict) and payload.get(key):
            return f'：{payload[key]}'
    return ''


def submit(session: Session, source: str, value: str, scope: str) -> None:
    if scope == 'instance':
        field = 'bilibili_cookie' if source == 'bilibili' else 'youtube_cookies'
        session.request('PATCH', '/api/admin/media', {field: value})
        return
    session.request('PUT', f'/api/source/credential/{source}', {'secret': value})


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def session_names(rows: list[dict[str, Any]], source: str) -> list[str]:
    """Login cookies this source would actually receive.

    Only cookies on the source's own host count: the login cookies a profile keeps
    for google.com are invisible to youtube.com, so counting them said "关键项 ✓"
    for a profile that was not logged into YouTube at all.
    """
    host = SESSION_HOST[source]
    expected = set(SOURCES[source]['expected'])
    return sorted({
        str(row['name'])
        for row in rows
        if str(row['host'] or '').lstrip('.').endswith(host) and str(row['name']) in expected
    })


def describe(rows: list[dict[str, Any]], source: str) -> str:
    """What was found, in names only — never a value."""
    names = [str(row['name']) for row in rows]
    found = session_names(rows, source)
    size = len(as_netscape_file(rows).encode('utf-8')) if source == 'youtube' else len(as_cookie_header(rows))
    parts = [f'{len(rows)} 个 cookie（{", ".join(sorted(names)[:6])}{"…" if len(names) > 6 else ""}）']
    if found:
        parts.append(f'{SESSION_HOST[source]} 上的登录项 {", ".join(found)} ✓')
    else:
        parts.append(
            f'⚠ {SESSION_HOST[source]} 上一个登录 cookie 都没有'
            f'（{"、".join(SOURCES[source]["expected"][:4])}… 都不在）：这个 profile 大概没登录'
            f' {SOURCES[source]["label"]}'
        )
    parts.append(f'{size / 1024:.1f} KB')
    return '，'.join(parts)


def _ask_password(username: str, server: str) -> str:
    """Prompt without echo. No terminal (a script, a CI job) needs the env var."""
    if not sys.stdin.isatty():
        # 直接说清楚：getpass 在这种情况下会退化成明文读，还先打一句吓人的警告。
        raise ScriptError('这里没有终端可以输入密码；改用环境变量 HIPPO_PASSWORD=… 或 --password 提供')
    try:
        return getpass.getpass(f'{username}@{server} 的密码（不回显、不落盘）：')
    except (EOFError, OSError) as exc:
        raise ScriptError(
            '这里没有终端可以输入密码；改用环境变量 HIPPO_PASSWORD=… 或 --password 提供'
        ) from exc


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--server', required=True, help='hippo 服务地址，如 https://hippo.example.com')
    parser.add_argument('--user', default='admin', help='hippo 用户名（默认 admin）')
    parser.add_argument(
        '--password',
        default=os.environ.get('HIPPO_PASSWORD', ''),
        help='hippo 密码；不给则交互输入（也可用环境变量 HIPPO_PASSWORD）',
    )
    parser.add_argument(
        '--scope',
        choices=('instance', 'user'),
        default='instance',
        help='instance 写入实例凭证（管理员，后台同步用；默认）；user 只写你自己的',
    )
    parser.add_argument('--profile', default='', help='Firefox profile 目录或名字片段')
    parser.add_argument('--sources', default='bilibili,youtube', help='逗号分隔，默认两个都提交')
    parser.add_argument('--dry-run', action='store_true', help='只看会提交什么，不实际提交')
    parser.add_argument('--insecure', action='store_true', help='忽略 TLS 证书错误（自签名证书时用）')
    return parser.parse_args(argv)


def resolve_profile(hint: str) -> Path:
    if hint:
        path = Path(hint).expanduser()
        if (path / 'cookies.sqlite').is_file():
            return path
        if path.is_dir():
            # 指到 profile 根目录（~/.mozilla/firefox）也算，省得记目录名。
            inside = [item for item in sorted(path.iterdir()) if (item / 'cookies.sqlite').is_file()]
            if len(inside) == 1:
                return inside[0]
            if inside:
                names = ', '.join(item.name for item in inside)
                raise ScriptError(f'{path} 下有多个 profile（{names}），用 --profile 指明一个')
        hint = path.name if path.is_dir() else hint
        for name, candidate in profiles():
            if hint in name:
                return candidate
        raise ScriptError(f'--profile {hint} 没匹配到任何 profile')
    return default_profile()


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    wanted = [name.strip() for name in args.sources.split(',') if name.strip()]
    unknown = [name for name in wanted if name not in SOURCES]
    if unknown:
        raise ScriptError(f'不认识的来源：{", ".join(unknown)}（可用：{", ".join(SOURCES)}）')

    profile = resolve_profile(args.profile)
    print(f'Firefox profile：{profile.name}（{profile}）')
    rows = read_cookies(profile)

    payloads: dict[str, str] = {}
    for source in wanted:
        selected = cookies_for(rows, source)
        print(f'{SOURCES[source]["label"]:9s}：{describe(selected, source)}')
        if not selected:
            print(f'          跳过：没有 {SOURCES[source]["domains"][0]} 的 cookie，先用浏览器登录一次')
            continue
        payloads[source] = as_netscape_file(selected) if source == 'youtube' else as_cookie_header(selected)

    if not payloads:
        raise ScriptError('没有任何可提交的 cookie')

    if args.dry_run:
        print(f'（dry-run）会以 {args.scope} 范围提交 {len(payloads)} 个来源，未实际发送')
        return 0

    session = Session(args.server, insecure=args.insecure)
    password = args.password or _ask_password(args.user, args.server)
    role = session.login(args.user, password)
    print(f'已登录 {args.user}{f"（{role}）" if role else ""}')
    if args.scope == 'instance' and role and role != 'admin':
        raise ScriptError(f'账号 {args.user} 不是管理员，实例凭证需要管理员；改用 --scope user')
    for source, value in payloads.items():
        try:
            submit(session, source, value, args.scope)
        except ScriptError as exc:
            if args.scope == 'instance' and '403' in str(exc):
                raise ScriptError(f'{exc}\n该账号不是管理员，改用 --scope user 只配置你自己的凭证') from exc
            raise
        print(f'已提交 {SOURCES[source]["label"]} 的凭证（{args.scope} 范围）')
    print('完成。凭证不回显；要清除就再跑一次并先清空浏览器 cookie，或用 --scope/设置页里的清除按钮。')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except ScriptError as error:
        print(f'错误：{error}', file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)
