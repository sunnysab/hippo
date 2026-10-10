import contextlib
import unittest
from unittest.mock import AsyncMock, patch

from hippo.media_settings import (
    default_media_settings,
    normalize_media_settings,
    set_media_settings,
)


class _DummyStorage:
    def transaction(self):
        return contextlib.nullcontext()


class MediaSettingsDefaultsTest(unittest.TestCase):
    def test_defaults_describe_the_two_tiers(self) -> None:
        settings = default_media_settings()

        self.assertTrue(settings['enabled'])
        # Accuracy is the default: a wrong transcript costs more than a slow one.
        self.assertEqual('accurate', settings['asr_tier'])
        self.assertEqual(
            {'accurate': 'http://192.168.130.202:8317/v1', 'fast': 'http://192.168.130.202:8318/v1'},
            settings['asr_endpoints'],
        )
        # A tail chunk shorter than this makes the decoder emit garbage.
        self.assertEqual(60, settings['min_tail_seconds'])
        self.assertGreater(settings['max_chunk_seconds'], settings['chunk_seconds'])


class MediaSettingsNormalizeTest(unittest.TestCase):
    def test_unknown_keys_are_dropped(self) -> None:
        settings = normalize_media_settings({'nope': 1, 'enabled': False})

        self.assertNotIn('nope', settings)
        self.assertFalse(settings['enabled'])

    def test_types_are_coerced_and_clamped(self) -> None:
        settings = normalize_media_settings(
            {
                'enabled': 'off',
                'items_per_sync': '999',
                'chunk_seconds': 1,
                'asr_timeout_seconds': 'nonsense',
                'keep_media_bitrate': 'flac',
            }
        )

        self.assertFalse(settings['enabled'])
        self.assertEqual(50, settings['items_per_sync'])
        self.assertEqual(30, settings['chunk_seconds'])
        self.assertEqual(900, settings['asr_timeout_seconds'])
        self.assertEqual('128k', settings['keep_media_bitrate'])

    def test_an_unknown_tier_falls_back_to_accurate(self) -> None:
        self.assertEqual('accurate', normalize_media_settings({'asr_tier': 'best'})['asr_tier'])

    def test_a_partial_endpoint_update_keeps_the_other_tier(self) -> None:
        settings = normalize_media_settings({'asr_endpoints': {'fast': 'http://other:9000/v1'}})

        self.assertEqual('http://other:9000/v1', settings['asr_endpoints']['fast'])
        self.assertEqual('http://192.168.130.202:8317/v1', settings['asr_endpoints']['accurate'])

    def test_a_blank_endpoint_disables_that_tier_on_purpose(self) -> None:
        settings = normalize_media_settings({'asr_endpoints': {'fast': ''}})

        self.assertEqual('', settings['asr_endpoints']['fast'])

    def test_a_junk_endpoint_is_replaced_by_the_default(self) -> None:
        settings = normalize_media_settings({'asr_endpoints': {'accurate': 'not a url'}})

        self.assertEqual('http://192.168.130.202:8317/v1', settings['asr_endpoints']['accurate'])

    def test_tail_and_ceiling_follow_the_chunk_length(self) -> None:
        settings = normalize_media_settings({'chunk_seconds': 120, 'min_tail_seconds': 300, 'max_chunk_seconds': 60})

        self.assertEqual(120, settings['min_tail_seconds'])
        # 上限必须给「把短尾并入前一片」留出空间（media/audio.py）。
        self.assertEqual(240, settings['max_chunk_seconds'])

    def test_secrets_are_stripped_but_kept(self) -> None:
        settings = normalize_media_settings({'asr_api_key': '  secret  ', 'bilibili_cookie': None})

        self.assertEqual('secret', settings['asr_api_key'])
        self.assertEqual('', settings['bilibili_cookie'])


class MediaSettingsStorageTest(unittest.IsolatedAsyncioTestCase):
    async def test_set_media_settings_saves_the_normalized_payload(self) -> None:
        storage = _DummyStorage()

        with (
            patch(
                'hippo.media_settings.get_media_settings',
                new=AsyncMock(return_value=default_media_settings()),
            ),
            patch('hippo.media_settings.save_meta_json', new=AsyncMock()) as save_meta_json,
        ):
            result = await set_media_settings(storage, {'items_per_sync': '7', 'bogus': True})

        self.assertEqual(7, result['items_per_sync'])
        saved = save_meta_json.call_args.args[2]
        self.assertNotIn('bogus', saved)
        self.assertEqual(7, saved['items_per_sync'])


if __name__ == '__main__':
    unittest.main()
