import contextlib
import unittest
from unittest.mock import AsyncMock, patch

from hippo.media_settings import (
    PROXY_DIRECT,
    PROXY_FOLLOW,
    default_media_settings,
    normalize_media_settings,
    resolve_proxy,
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


class ProxySettingsTest(unittest.TestCase):
    """Each source decides for itself whether to use the instance proxy."""

    def test_defaults_keep_bilibili_direct_and_youtube_proxied(self) -> None:
        settings = default_media_settings()

        # B站 会把代理出口的 IP 拉黑（实测 412），所以它必须显式直连——这样即使管理员
        # 之后配了默认代理，B站 也不会跟着走。
        self.assertEqual(PROXY_DIRECT, settings['proxy_bilibili'])
        self.assertEqual('', resolve_proxy(settings, 'bilibili'))
        self.assertTrue(resolve_proxy(settings, 'youtube'))
        # 播客默认跟随实例默认值，而默认值是空的，于是直连。
        self.assertEqual(PROXY_FOLLOW, settings['proxy_podcast'])
        self.assertEqual('', resolve_proxy(settings, 'podcast'))

    def test_a_default_proxy_is_picked_up_by_the_sources_that_follow_it(self) -> None:
        settings = normalize_media_settings({'proxy_default': 'http://proxy.example:8082'})

        self.assertEqual('http://proxy.example:8082', resolve_proxy(settings, 'podcast'))
        # 跟随默认的会被默认覆盖，显式直连/显式代理的不受影响。
        self.assertEqual('', resolve_proxy(settings, 'bilibili'))
        self.assertEqual('http://100.111.170.110:8082', resolve_proxy(settings, 'youtube'))

    def test_a_source_can_override_the_default_with_its_own_proxy(self) -> None:
        settings = normalize_media_settings(
            {'proxy_default': 'http://proxy.example:8082', 'proxy_podcast': 'socks5://127.0.0.1:1080'}
        )

        self.assertEqual('socks5://127.0.0.1:1080', resolve_proxy(settings, 'podcast'))

    def test_a_source_can_go_back_to_direct_with_the_sentinel(self) -> None:
        settings = normalize_media_settings(
            {'proxy_default': 'http://proxy.example:8082', 'proxy_podcast': 'direct'}, current=None
        )

        self.assertEqual('', resolve_proxy(settings, 'podcast'))

    def test_a_missing_source_follows_the_default(self) -> None:
        # 旧配置里没有 podcast 键时不应该是「直连」，而是「跟随默认」。
        self.assertEqual('http://proxy.example:8082', resolve_proxy({'proxy_default': 'http://proxy.example:8082'}, 'podcast'))

    def test_a_typo_keeps_the_previous_value_instead_of_going_direct(self) -> None:
        current = normalize_media_settings({'proxy_podcast': 'http://proxy.example:8082'})

        settings = normalize_media_settings({'proxy_podcast': 'htp://proxy.example:8082'}, current=current)

        self.assertEqual('http://proxy.example:8082', settings['proxy_podcast'])

    def test_the_default_proxy_is_never_a_sentinel(self) -> None:
        settings = normalize_media_settings({'proxy_default': 'direct'})

        self.assertEqual('', settings['proxy_default'])
        self.assertEqual('', resolve_proxy(settings, 'podcast'))


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
