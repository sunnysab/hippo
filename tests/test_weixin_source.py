from __future__ import annotations

import asyncio
import unittest

from hippo.weixin_source import WeixinSource


class _Bot:
    async def call(self, action, params, *, timeout):
        assert action == 'get_article_bodies'
        return {
            'articles': [
                {
                    'url': params['urls'][0],
                    'short_link': 'https://mp.weixin.qq.com/s/slug',
                    'slug': 'slug',
                    'html': '<p>body</p>',
                }
            ],
            'errors': [
                {'urls': [params['urls'][1]], 'error': '6771 empty; 2594 returned no body'},
            ],
        }


class WeixinSourceBodyDiagnosticsTest(unittest.TestCase):
    def test_preserves_per_url_daemon_diagnostics(self) -> None:
        source = WeixinSource(auto_login=False)
        source._bot = _Bot()
        urls = ['https://mp.weixin.qq.com/s/ok', 'https://mp.weixin.qq.com/s/missing']

        bodies = asyncio.run(source.fetch_bodies(urls))

        self.assertEqual([body.url for body in bodies], [urls[0]])
        self.assertIsNone(source.body_error_for(urls[0]))
        self.assertEqual(source.body_error_for(urls[1]), '6771 empty; 2594 returned no body')


if __name__ == '__main__':
    unittest.main()
