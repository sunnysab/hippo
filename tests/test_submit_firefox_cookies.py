"""The Firefox cookie submitter: what it picks, and what it must never print.

The privacy rule is the point of this script, so it is asserted directly: a
sentinel value is planted in the fake cookie database and every surface a person
can see — stdout, stderr, a failed request — is checked for it.
"""

import contextlib
import importlib.util
import io
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).resolve().parent.parent / 'scripts' / 'submit_firefox_cookies.py'

#: 只要出现在任何输出里，测试就失败。
SENTINEL = 'SENTINEL-cookie-value-9f3a'


def _load():
    spec = importlib.util.spec_from_file_location('submit_firefox_cookies', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


submitter = _load()


def _profile(directory: str, rows) -> Path:
    """Build a minimal Firefox profile: profiles.ini plus a cookie database."""
    profile = Path(directory) / 'abc.default-release'
    profile.mkdir(parents=True, exist_ok=True)
    Path(directory, 'profiles.ini').write_text(
        '[Profile0]\nName=default-release\nIsRelative=1\nPath=abc.default-release\nDefault=1\n',
        encoding='utf-8',
    )
    connection = sqlite3.connect(profile / 'cookies.sqlite')
    connection.execute(
        'CREATE TABLE moz_cookies (host TEXT, name TEXT, value TEXT, path TEXT, '
        "expiry INTEGER, isSecure INTEGER, isHttpOnly INTEGER, originAttributes TEXT DEFAULT '')"
    )
    connection.executemany('INSERT INTO moz_cookies VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                           [(*tuple(row), '') if len(row) == 7 else tuple(row) for row in rows])
    connection.commit()
    connection.close()
    return profile


def _row(tuple_row) -> dict:
    host, name, value, path, expiry, secure, http_only = tuple_row
    return {
        'host': host, 'name': name, 'value': value, 'path': path,
        'expiry': expiry, 'isSecure': secure, 'isHttpOnly': http_only, 'originAttributes': '',
    }


def _rows():
    future = 4102444800
    return [
        ('.bilibili.com', 'SESSDATA', SENTINEL, '/', future, 1, 1),
        ('.bilibili.com', 'bili_jct', 'jct', '/', future, 0, 0),
        ('www.bilibili.com', 'buvid3', 'buvid', '/', future, 0, 0),
        ('.bilibili.com', 'stale', 'gone', '/', 1000000000, 0, 0),
        ('.youtube.com', 'SID', SENTINEL, '/', future, 1, 1),
        ('.google.com', 'SAPISID', 'sapisid', '/', 0, 1, 0),
        ('evil.example', 'SESSDATA', SENTINEL, '/', future, 0, 0),
        ('.bilibili.com', 'broken', 'has\ttab', '/', future, 0, 0),
    ]


class CookieSelectionTest(unittest.TestCase):
    def test_only_the_sources_own_live_cookies_are_kept(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            profile = _profile(directory, _rows())
            rows = submitter.read_cookies(profile)

        names = sorted(row['name'] for row in submitter.cookies_for(rows, 'bilibili'))
        # 过期、别的域、值里带制表符的都不要。
        self.assertEqual(['SESSDATA', 'bili_jct', 'buvid3'], names)
        youtube = sorted(row['name'] for row in submitter.cookies_for(rows, 'youtube'))
        self.assertEqual(['SAPISID', 'SID'], youtube)

    def test_the_bilibili_credential_is_one_cookie_header(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            rows = submitter.cookies_for(submitter.read_cookies(_profile(directory, _rows())), 'bilibili')

        header = submitter.as_cookie_header(rows)

        self.assertEqual(f'SESSDATA={SENTINEL}; bili_jct=jct; buvid3=buvid', header)
        self.assertNotIn('\n', header)

    def test_the_youtube_credential_is_a_netscape_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            rows = submitter.cookies_for(submitter.read_cookies(_profile(directory, _rows())), 'youtube')

        text = submitter.as_netscape_file(rows)
        lines = text.splitlines()

        self.assertEqual('# Netscape HTTP Cookie File', lines[0])
        # HttpOnly 的前缀是 yt-dlp 认它的方式；域名前导点 = 包含子域。
        self.assertIn(f'#HttpOnly_.youtube.com\tTRUE\t/\tTRUE\t4102444800\tSID\t{SENTINEL}', text)
        # 会话 cookie（expiry=0）保留为 0，交给 yt-dlp 当会话处理。
        self.assertIn('.google.com\tTRUE\t/\tTRUE\t0\tSAPISID\tsapisid', text)
        # #HttpOnly_ 也是数据行，只是带前缀；数据行都含制表符。
        self.assertEqual(len([line for line in lines if '\t' in line]), 2)

    def test_a_profile_is_found_through_profiles_ini(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            profile = _profile(directory, _rows())
            with mock.patch.object(submitter, 'PROFILE_ROOTS', (Path(directory),)):
                found = submitter.default_profile()

        self.assertEqual(profile, found)


class SessionHostTest(unittest.TestCase):
    """A login cookie counts only where the source would actually receive it."""

    def test_google_login_cookies_do_not_count_as_a_youtube_login(self) -> None:
        rows = [
            *(row for row in _rows() if row[1] in ('SID', 'SAPISID') and row[0] == '.google.com'),
            ('.youtube.com', 'PREF', 'x', '/', 4102444800, 0, 0),
        ]
        described = submitter.describe([_row(row) for row in rows], 'youtube')

        self.assertIn('一个登录 cookie 都没有', described)
        # 名字仍然报出来（它确实存在），但不会当成 YouTube 的登录项。
        self.assertEqual([], submitter.session_names([_row(row) for row in rows], 'youtube'))

    def test_a_youtube_login_cookie_does_count(self) -> None:
        rows = [*_rows(), ('.youtube.com', 'LOGIN_INFO', 'x', '/', 4102444800, 1, 1)]
        described = submitter.describe([_row(row) for row in rows], 'youtube')

        self.assertIn('youtube.com 上的登录项', described)
        self.assertNotIn('一个登录 cookie 都没有', described)

    def test_bilibili_counts_only_its_own_host(self) -> None:
        rows = [('.bilibili.com', 'SESSDATA', 'x', '/', 4102444800, 1, 1)]

        self.assertEqual(['SESSDATA'], submitter.session_names([_row(row) for row in rows], 'bilibili'))


class PrivacyTest(unittest.TestCase):
    """Nothing a person can see may contain a cookie value."""

    def _run(self, argv):
        """Run like the command line does, and hand back everything it printed."""
        out, err = io.StringIO(), io.StringIO()
        code = 0
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = submitter.main(argv)
            except submitter.ScriptError as error:
                print(f'错误：{error}', file=err)
                code = 1
        return out.getvalue() + err.getvalue(), code

    def test_a_dry_run_prints_names_and_sizes_but_no_value(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            profile = _profile(directory, _rows())
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = submitter.main(
                    ['--server', 'https://hippo.example', '--profile', str(profile), '--dry-run']
                )

        printed = out.getvalue()
        self.assertEqual(0, code)
        self.assertNotIn(SENTINEL, printed + err.getvalue())
        self.assertIn('SESSDATA', printed)  # 名字可以出现，值不行
        self.assertIn('dry-run', printed)

    def test_no_terminal_says_how_to_pass_the_password(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _profile(directory, _rows())
            with mock.patch.object(submitter.getpass, 'getpass', side_effect=EOFError):
                printed, code = self._run(
                    ['--server', 'http://127.0.0.1:9', '--profile', str(Path(directory) / 'abc.default-release')]
                )

        self.assertEqual(1, code)
        self.assertIn('HIPPO_PASSWORD', printed)
        self.assertNotIn(SENTINEL, printed)

    def test_a_failed_login_does_not_leak_the_cookie(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _profile(directory, _rows())
            printed, code = self._run(
                [
                    '--server', 'http://127.0.0.1:9',
                    '--profile', str(Path(directory) / 'abc.default-release'),
                    '--password', 'not-a-real-password',
                    '--scope', 'user',
                ]
            )

        self.assertEqual(1, code)
        self.assertNotIn(SENTINEL, printed)
        self.assertIn('错误：', printed)


if __name__ == '__main__':
    unittest.main()
