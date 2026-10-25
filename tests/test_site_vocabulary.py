"""The vocabulary version is the cache switch: it moves only when topics change."""

import json
import unittest
from unittest.mock import AsyncMock, patch

from hippo import site_settings
from hippo.exceptions import ApiError


def _storage(meta: dict | None = None):
    """A storage double that really round-trips the meta table."""
    store = dict(meta or {})

    class _Meta:
        async def get(self, key):
            return store.get(key)

        async def set(self, key, value):
            store[key] = value

    class _Transaction:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    return type(
        'Storage',
        (),
        {'meta': _Meta(), 'transaction': lambda self: _Transaction(), '_store': store},
    )()


class VocabularyDefaultsTest(unittest.IsolatedAsyncioTestCase):
    async def test_a_fresh_instance_gets_the_default_vocabulary(self):
        settings = await site_settings.get_site_settings(_storage())
        self.assertEqual(1, settings['vocabulary']['version'])
        self.assertTrue(settings['vocabulary']['topics'])

    async def test_an_unreadable_stored_vocabulary_falls_back_to_the_default(self):
        for broken in ({'version': 0, 'topics': []}, {'topics': 'ai'}, 'nonsense', []):
            with self.subTest(broken=broken):
                settings = await site_settings.get_site_settings(
                    _storage({site_settings.SITE_SETTINGS_KEY: json.dumps({'vocabulary': broken})})
                )
                self.assertEqual(site_settings.default_vocabulary(), settings['vocabulary'])


class VocabularyVersionTest(unittest.IsolatedAsyncioTestCase):
    async def test_saving_the_same_topics_keeps_the_version(self):
        storage = _storage()
        await site_settings.set_site_settings(storage, {'vocabulary': site_settings.default_vocabulary()})
        settings = await site_settings.get_site_settings(storage)
        self.assertEqual(1, settings['vocabulary']['version'])

    async def test_editing_the_topics_bumps_the_version_once_per_change(self):
        storage = _storage()
        smaller = site_settings.default_vocabulary()
        smaller['topics'] = smaller['topics'][:3]

        settings = await site_settings.set_site_settings(storage, {'vocabulary': smaller})
        self.assertEqual(2, settings['vocabulary']['version'])
        # Saving the same edited list again must not keep inflating it.
        settings = await site_settings.set_site_settings(storage, {'vocabulary': smaller})
        self.assertEqual(2, settings['vocabulary']['version'])

        settings = await site_settings.set_site_settings(storage, {'vocabulary': site_settings.default_vocabulary()})
        self.assertEqual(3, settings['vocabulary']['version'])

    async def test_a_client_supplied_version_is_ignored(self):
        storage = _storage()
        submitted = site_settings.default_vocabulary()
        submitted['version'] = 99
        settings = await site_settings.set_site_settings(storage, {'vocabulary': submitted})
        self.assertEqual(1, settings['vocabulary']['version'])

    async def test_a_duplicated_slug_is_refused_and_nothing_is_written(self):
        storage = _storage()
        before = await site_settings.get_site_settings(storage)
        broken = site_settings.default_vocabulary()
        broken['topics'].append(dict(broken['topics'][0]))

        with self.assertRaises(ApiError) as ctx:
            await site_settings.set_site_settings(storage, {'vocabulary': broken})

        self.assertEqual('topic.invalid_vocabulary', ctx.exception.code)
        self.assertEqual(before['vocabulary'], (await site_settings.get_site_settings(storage))['vocabulary'])

    async def test_other_settings_still_round_trip(self):
        storage = _storage()
        await site_settings.set_site_settings(storage, {'site_name': 'Name', 'public_base_url': 'https://x/'})
        settings = await site_settings.get_site_settings(storage)
        self.assertEqual('Name', settings['site_name'])
        self.assertEqual('https://x', settings['public_base_url'])
        self.assertEqual(1, settings['vocabulary']['version'])


class AdminRouteTest(unittest.IsolatedAsyncioTestCase):
    async def test_editing_the_vocabulary_is_audited_like_any_other_key(self):
        from hippo.api.routers import admin as admin_api
        from hippo.api.schemas.admin import SiteSettingsUpdateRequest

        storage = _storage()
        storage.audit = AsyncMock()
        lighter = site_settings.default_vocabulary()
        lighter['topics'] = lighter['topics'][:2]
        settings = await site_settings.set_site_settings(storage, {'vocabulary': lighter})
        storage.audit.reset_mock()

        with (
            patch.object(admin_api, 'get_site_settings', AsyncMock(return_value={**settings, 'vocabulary': lighter})),
            patch.object(admin_api, 'set_site_settings', AsyncMock(return_value=settings)),
        ):
            await admin_api.update_site_settings(
                type('R', (), {'headers': {}, 'client': None})(),
                body=SiteSettingsUpdateRequest(vocabulary=settings['vocabulary']),
                actor=type('U', (), {'id': 1})(),
                storage=storage,
            )

        self.assertEqual('admin.site_settings_updated', storage.audit.record.await_args.args[1])


if __name__ == '__main__':
    unittest.main()
