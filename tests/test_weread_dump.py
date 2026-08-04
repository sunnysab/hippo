import sqlite3
import tempfile
import unittest
from pathlib import Path

from hippo.models import LoginSession
from hippo.weread_dump import WereadDumpError, load_credential, load_device_id

_DEVICE_XML = """<?xml version='1.0' encoding='utf-8' standalone='yes' ?>
<map>
    <string name="deviceid">35001432113348933722723500921632438154</string>
    <int name="app_version" value="10167607" />
</map>
"""


def _write_account_db(dump_dir: Path, rows: list[dict]) -> None:
    db_path = dump_dir / 'WRAccount'
    conn = sqlite3.connect(db_path)
    conn.execute(
        'CREATE TABLE Account ('
        'id INTEGER PRIMARY KEY, vid TEXT, accessToken TEXT, '
        'refreshToken TEXT, guestLogin INTEGER DEFAULT 0)'
    )
    conn.executemany(
        'INSERT INTO Account (id, vid, accessToken, refreshToken, guestLogin) VALUES (?, ?, ?, ?, ?)',
        [
            (
                row.get('id', i + 1),
                row['vid'],
                row['accessToken'],
                row.get('refreshToken', ''),
                int(row.get('guestLogin', False)),
            )
            for i, row in enumerate(rows)
        ],
    )
    conn.commit()
    conn.close()


class WereadDumpTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dump_dir = Path(self.tmp.name)
        (self.dump_dir / 'device.xml').write_text(_DEVICE_XML, encoding='utf-8')

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_load_credential_newest_non_guest(self) -> None:
        _write_account_db(
            self.dump_dir,
            [
                {'vid': '100', 'accessToken': 'guest-token', 'guestLogin': True},
                {'vid': '200', 'accessToken': 'real-token', 'refreshToken': 'rt-200'},
                {'vid': '300', 'accessToken': '', 'refreshToken': ''},
            ],
        )
        session = load_credential(self.dump_dir)
        self.assertEqual(session.vid, '200')
        self.assertEqual(session.access_token, 'real-token')
        self.assertEqual(session.refresh_token, 'rt-200')
        self.assertEqual(session.device_id, '35001432113348933722723500921632438154')

    def test_load_credential_by_vid(self) -> None:
        _write_account_db(
            self.dump_dir,
            [
                {'vid': '200', 'accessToken': 'real-token'},
                {'vid': '999', 'accessToken': 'other-token'},
            ],
        )
        session = load_credential(self.dump_dir, vid='999')
        self.assertEqual(session.vid, '999')
        self.assertEqual(session.access_token, 'other-token')

    def test_load_credential_missing_db(self) -> None:
        with self.assertRaises(WereadDumpError):
            load_credential(self.dump_dir)

    def test_load_credential_no_usable_account(self) -> None:
        _write_account_db(self.dump_dir, [{'vid': '1', 'accessToken': '', 'guestLogin': True}])
        with self.assertRaises(WereadDumpError):
            load_credential(self.dump_dir)

    def test_load_device_id_missing_returns_empty(self) -> None:
        empty_dir = Path(tempfile.mkdtemp())
        try:
            self.assertEqual(load_device_id(empty_dir), '')
        finally:
            empty_dir.rmdir()

    def test_load_credential_reads_device_xml_under_shared_prefs(self) -> None:
        (self.dump_dir / 'device.xml').unlink()
        shared = self.dump_dir / 'shared_prefs'
        shared.mkdir()
        (shared / 'device.xml').write_text(_DEVICE_XML, encoding='utf-8')
        _write_account_db(self.dump_dir, [{'vid': '200', 'accessToken': 'real-token'}])
        session = load_credential(self.dump_dir)
        self.assertEqual(session.device_id, '35001432113348933722723500921632438154')


class LoginSessionModelTest(unittest.TestCase):
    def test_default_optional_fields(self) -> None:
        session = LoginSession(vid='200', access_token='real-token')
        self.assertEqual(session.refresh_token, '')
        self.assertEqual(session.device_id, '')
        self.assertIsNone(session.nickname)

    def test_extra_fields_ignored(self) -> None:
        session = LoginSession(vid='200', access_token='real-token', token='legacy', cookies={})
        self.assertFalse(hasattr(session, 'token'))


if __name__ == '__main__':
    unittest.main()
