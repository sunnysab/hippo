"""Load WeRead account credentials exported from the Android app.

The reference dump (``weread_mp``) stores credentials in a ``WRAccount``
SQLite database plus a ``device.xml`` shared-prefs file. This module reads
those into a :class:`~hippo.models.LoginSession` for import into Postgres.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from xml.etree import ElementTree

from .models import LoginSession


class WereadDumpError(RuntimeError):
    """Raised when the WeRead account dump cannot be read."""


def _find_account_db(dump_dir: Path) -> Path:
    for candidate in (dump_dir / 'WRAccount', dump_dir / 'databases' / 'WRAccount'):
        if candidate.is_file():
            return candidate
    raise WereadDumpError(f'WRAccount database not found under {dump_dir}')


def _find_device_file(dump_dir: Path) -> Path | None:
    for candidate in (dump_dir / 'device.xml', dump_dir / 'shared_prefs' / 'device.xml'):
        if candidate.is_file():
            return candidate
    return None


def load_device_id(dump_dir: str | Path) -> str:
    device_file = _find_device_file(Path(dump_dir).expanduser())
    if device_file is None:
        return ''
    try:
        root = ElementTree.parse(device_file).getroot()
    except ElementTree.ParseError as exc:
        raise WereadDumpError(f'Cannot parse device.xml: {exc}') from exc
    for child in root:
        if child.tag == 'string' and child.attrib.get('name') == 'deviceid':
            value = (child.text or '').strip()
            if value:
                return value
    return ''


def load_credential(dump_dir: str | Path, *, vid: str | None = None) -> LoginSession:
    """Read vid/accessToken/refreshToken/deviceId from a WeRead dump.

    When *vid* is omitted the newest non-guest account with a usable access
    token is selected, mirroring ``weread_mp``.
    """
    base = Path(dump_dir).expanduser()
    account_db = _find_account_db(base)
    try:
        connection = sqlite3.connect(account_db)
        connection.row_factory = sqlite3.Row
        if vid:
            row = connection.execute(
                'SELECT vid, accessToken, refreshToken FROM Account '
                'WHERE vid = ? AND accessToken IS NOT NULL AND accessToken <> ? '
                'LIMIT 1',
                (vid, ''),
            ).fetchone()
        else:
            row = connection.execute(
                'SELECT vid, accessToken, refreshToken FROM Account '
                'WHERE accessToken IS NOT NULL AND accessToken <> ? '
                'AND COALESCE(guestLogin, 0) = 0 '
                'ORDER BY id DESC LIMIT 1',
                ('',),
            ).fetchone()
    except sqlite3.Error as exc:
        raise WereadDumpError(f'Cannot read account database: {exc}') from exc
    finally:
        if 'connection' in locals():
            connection.close()

    if row is None:
        selected = f'vid {vid!r}' if vid else 'a non-guest account'
        raise WereadDumpError(f'No usable access token found for {selected}')

    account_vid = str(row['vid'])
    access_token = str(row['accessToken'])
    if not account_vid or not access_token:
        raise WereadDumpError('The selected account has an empty vid or access token')
    return LoginSession(
        vid=account_vid,
        access_token=access_token,
        refresh_token=str(row['refreshToken'] or ''),
        device_id=load_device_id(base),
    )


__all__ = ['WereadDumpError', 'load_credential', 'load_device_id']
