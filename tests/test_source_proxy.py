"""Each source names its own proxy; there is no instance-wide one.

A WeChat fetch and a YouTube fetch must not share one exit: WeChat is reachable
directly, YouTube has to leave through the operator's proxy, and the proxy that
used to sit in ``[http] proxy`` belonged to a single source anyway.
"""

import unittest

from hippo.container import _source_proxy
from hippo.media_settings import PROXY_SOURCES, default_media_settings, resolve_proxy


class ResolveProxyTest(unittest.TestCase):
    def test_weixin_defaults_to_a_direct_connection(self) -> None:
        # The WeChat hosts answer directly; a proxy here only adds a hop.
        self.assertEqual('', resolve_proxy(default_media_settings(), 'weixin'))

    def test_youtube_defaults_to_the_operator_proxy(self) -> None:
        self.assertEqual(
            'http://100.111.170.110:8082',
            resolve_proxy(default_media_settings(), 'youtube'),
        )

    def test_weixin_is_a_proxy_source(self) -> None:
        self.assertIn('weixin', PROXY_SOURCES)

    def test_a_source_own_value_wins_over_the_default(self) -> None:
        settings = {**default_media_settings(), 'proxy_default': 'http://d:1', 'proxy_bilibili': 'http://b:2'}
        self.assertEqual('http://b:2', resolve_proxy(settings, 'bilibili'))

    def test_follow_uses_the_instance_default(self) -> None:
        settings = {**default_media_settings(), 'proxy_default': 'http://d:1', 'proxy_podcast': 'default'}
        self.assertEqual('http://d:1', resolve_proxy(settings, 'podcast'))

    def test_direct_sentinel_overrides_a_default(self) -> None:
        settings = {**default_media_settings(), 'proxy_default': 'http://d:1', 'proxy_bilibili': 'direct'}
        self.assertEqual('', resolve_proxy(settings, 'bilibili'))

    def test_container_helper_falls_back_to_direct_without_settings(self) -> None:
        # A test or an early boot has no settings loaded; connect directly rather
        # than guess an exit.
        self.assertEqual('', _source_proxy(None, 'weixin'))
        self.assertEqual('', _source_proxy({}, 'youtube'))

    def test_container_helper_sends_an_unknown_source_to_the_default(self) -> None:
        settings = {**default_media_settings(), 'proxy_default': 'http://d:1'}
        self.assertEqual('http://d:1', _source_proxy(settings, 'mastodon'))


class ImageRouteQueryTest(unittest.TestCase):
    def test_the_image_row_query_selects_the_account_source(self) -> None:
        # The proxy is chosen from the article's account source, so the fetch query
        # has to carry it; without it every image would share one exit.
        import inspect

        from hippo import image_hashes

        source = inspect.getsource(image_hashes.fetch_image_bytes)
        self.assertIn('acc.source AS account_source', source)
        self.assertIn('LEFT JOIN accounts acc ON acc.biz = a.biz', source)


if __name__ == '__main__':
    unittest.main()
