"""The test-email button maps a mail-server failure onto the right HTTP status.

A failing SMTP server is not the caller's mistake, so it must not read as a 400:
the timeout is a 504, and anything else the server says is a 502.
"""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from hippo.api.routers import settings as settings_api
from hippo.exceptions import ApiError


def _storage() -> SimpleNamespace:
    return SimpleNamespace(
        transaction=lambda: _NullTransaction(),
        meta=AsyncMock(),
    )


class _NullTransaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _user() -> SimpleNamespace:
    return SimpleNamespace(id=1, username='admin', role='admin')


class TestEmailFailureTest(unittest.IsolatedAsyncioTestCase):
    async def _send(self, *, error: Exception) -> ApiError:
        with (
            patch.object(settings_api, 'get_email_settings', new=AsyncMock(return_value={'smtp_host': 'smtp.local'})),
            patch.object(settings_api, 'send_email', side_effect=error),
            self.assertRaises(ApiError) as ctx,
        ):
            await settings_api.send_sync_test_email(
                body={'to_email': 'ops@example.com'},
                storage=_storage(),
                _=_user(),
            )
        return ctx.exception

    async def test_a_mail_server_failure_is_a_bad_gateway(self) -> None:
        exc = await self._send(error=RuntimeError('SMTPAuthenticationError: 535 bad credentials'))

        self.assertEqual(502, exc.status)
        self.assertEqual('settings.email_failed', exc.code)
        # The button exists to say what the server said.
        self.assertIn('535', str(exc))

    async def test_a_timeout_is_a_gateway_timeout(self) -> None:
        exc = await self._send(error=TimeoutError('no answer within 20s'))

        self.assertEqual(504, exc.status)
        self.assertEqual('settings.email_timeout', exc.code)
