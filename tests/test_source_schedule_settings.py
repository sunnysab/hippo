"""Per-source polling settings and API validation."""

import contextlib
import unittest
from unittest.mock import AsyncMock, patch

from pydantic import ValidationError

from hippo.api.schemas.settings import SyncSettingsUpdateRequest
from hippo.sync_settings import POLLING_SOURCES, default_sync_settings, get_sync_settings, set_sync_settings


class _Storage:
    def transaction(self):
        return contextlib.nullcontext()


class SourceScheduleSettingsTest(unittest.IsolatedAsyncioTestCase):
    def test_defaults_poll_only_non_wechat_sources_daily(self) -> None:
        settings = default_sync_settings()
        self.assertEqual(settings['source_intervals_days'], dict.fromkeys(POLLING_SOURCES, 1))
        self.assertNotIn('weixin', settings['source_intervals_days'])
        self.assertNotIn('enabled', settings)
        self.assertNotIn('interval_minutes', settings)

    async def test_legacy_global_settings_do_not_disable_source_polling(self) -> None:
        stored = {'enabled': False, 'interval_minutes': 60, 'source_intervals_days': {'youtube': 3}}
        with patch('hippo.sync_settings.load_meta_json', new=AsyncMock(return_value=stored)):
            settings = await get_sync_settings(_Storage())
        self.assertEqual(settings['source_intervals_days'], {**dict.fromkeys(POLLING_SOURCES, 1), 'youtube': 3})
        self.assertNotIn('enabled', settings)
        self.assertNotIn('interval_minutes', settings)

    async def test_invalid_stored_intervals_fall_back_to_daily(self) -> None:
        stored = {'source_intervals_days': {'weixin': 2, 'feed': True, 'youtube': 0, 'podcast': '7'}}
        with patch('hippo.sync_settings.load_meta_json', new=AsyncMock(return_value=stored)):
            settings = await get_sync_settings(_Storage())
        self.assertEqual(settings['source_intervals_days'], dict.fromkeys(POLLING_SOURCES, 1))

    async def test_partial_update_preserves_other_source_intervals(self) -> None:
        stored = {'source_intervals_days': {'youtube': 7, 'feed': 3}}
        save = AsyncMock()
        with (
            patch('hippo.sync_settings.load_meta_json', new=AsyncMock(return_value=stored)),
            patch('hippo.sync_settings.save_meta_json', new=save),
        ):
            settings = await set_sync_settings(_Storage(), {'source_intervals_days': {'feed': 1}})
        self.assertEqual(settings['source_intervals_days'], {**dict.fromkeys(POLLING_SOURCES, 1), 'youtube': 7})
        self.assertEqual(save.await_args.args[2], settings)

    def test_request_accepts_a_partial_source_map(self) -> None:
        request = SyncSettingsUpdateRequest.model_validate({'source_intervals_days': {'feed': 2}})
        self.assertEqual(request.source_intervals_days, {'feed': 2})

    def test_caixin_defaults_to_daily_and_accepts_a_custom_interval(self) -> None:
        self.assertEqual(1, default_sync_settings()['source_intervals_days']['caixin'])
        request = SyncSettingsUpdateRequest.model_validate({'source_intervals_days': {'caixin': 3}})
        self.assertEqual(request.source_intervals_days, {'caixin': 3})

    def test_request_rejects_unknown_sources_and_invalid_days(self) -> None:
        for intervals in (
            {'weixin': 1},
            {'rss': 1},
            {'feed': 0},
            {'feed': -1},
            {'feed': True},
            {'feed': '2'},
            {'feed': 1.5},
        ):
            with self.subTest(intervals=intervals), self.assertRaises(ValidationError):
                SyncSettingsUpdateRequest.model_validate({'source_intervals_days': intervals})

    def test_request_rejects_retired_global_schedule_fields(self) -> None:
        for payload in ({'enabled': True}, {'interval_minutes': 60}):
            with self.subTest(payload=payload), self.assertRaises(ValidationError):
                SyncSettingsUpdateRequest.model_validate(payload)
