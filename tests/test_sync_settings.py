import contextlib
import unittest
from unittest.mock import patch

from hippo.sync_settings import default_sync_settings, set_sync_settings


class _DummyStorage:
    def transaction(self):
        return contextlib.nullcontext()


class SyncSettingsTest(unittest.TestCase):
    def test_default_sync_settings_include_article_exclude_keywords(self) -> None:
        settings = default_sync_settings()

        self.assertIn('article_exclude_keywords', settings)
        self.assertEqual('', settings['article_exclude_keywords'])

    def test_set_sync_settings_normalizes_article_exclude_keywords(self) -> None:
        storage = _DummyStorage()

        with (
            patch('hippo.sync_settings.get_sync_settings', return_value=default_sync_settings()),
            patch('hippo.sync_settings.save_meta_json') as save_meta_json,
        ):
            result = set_sync_settings(
                storage,
                {'article_exclude_keywords': ' Promo ; Ad \npromo\n\n'},
            )

        self.assertEqual('Promo\nAd', result['article_exclude_keywords'])
        saved_payload = save_meta_json.call_args.args[2]
        self.assertEqual('Promo\nAd', saved_payload['article_exclude_keywords'])


if __name__ == '__main__':
    unittest.main()
