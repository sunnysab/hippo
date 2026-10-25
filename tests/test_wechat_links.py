"""WeChat URL identity and extraction at the untrusted-content boundary."""

import unittest

from hippo.wechat_links import extract_wechat_links, parse_wechat_link

LONG = 'https://mp.weixin.qq.com/s?__biz=Mz1&mid=123&idx=2&sn=abc'


class WechatLinkTest(unittest.TestCase):
    def test_share_parameters_do_not_change_identity(self):
        first = parse_wechat_link(LONG)
        second = parse_wechat_link(LONG.replace('https:', 'http:') + '&scene=21#wechat_redirect')
        self.assertEqual('sn:Mz1:abc', first.identity)
        self.assertEqual(first.identity, second.identity)
        self.assertEqual('abc', first.queue_sn)
        self.assertTrue(second.url.startswith('https://'))

    def test_short_and_protocol_relative_links_have_a_slug_identity(self):
        link = parse_wechat_link('//mp.weixin.qq.com/s/Ab_c-12?scene=21')
        self.assertEqual('slug:Ab_c-12', link.identity)
        self.assertEqual('', link.biz)
        self.assertEqual('slug:Ab_c-12', link.queue_sn)

    def test_old_links_and_links_without_sn_use_mid_and_idx(self):
        link = parse_wechat_link('https://mp.weixin.qq.com/mp/appmsg/show?__biz=Mz1&appmsgid=123&itemidx=2')
        self.assertEqual('mid:Mz1:123:2', link.identity)
        self.assertEqual(link.identity, parse_wechat_link(LONG.split('&sn=')[0]).identity)

    def test_html_entities_are_decoded_without_losing_fetch_parameters(self):
        link = parse_wechat_link(LONG.replace('&', '&amp;') + '&amp;chksm=secret')
        self.assertEqual('sn:Mz1:abc', link.identity)
        self.assertIn('&chksm=secret', link.url)

    def test_untrusted_hosts_paths_and_ambiguous_identities_are_rejected(self):
        for value in (
            'javascript:alert(1)',
            'https://mp.weixin.qq.com.evil.test/s/slug',
            'https://mp.weixin.qq.com@evil.test/s/slug',
            'https://user@mp.weixin.qq.com/s/slug',
            'https://mp.weixin.qq.com:9090/s/slug',
            'https://mp.weixin.qq.com/s/one/two',
            'https://mp.weixin.qq.com/',
            LONG.replace('/s?', '/other?'),
            LONG + '&sn=other',
            LONG.replace('/s?', '/s\n?'),
            'https://mp.weixin.qq.com\\@evil.test/s/slug',
            'https://mp.weixin.qq.com/s?__biz=Mz1&mid=1&idx=0',
        ):
            self.assertIsNone(parse_wechat_link(value), value)

    def test_nested_links_bare_urls_and_block_hrefs_are_extracted(self):
        short = 'https://mp.weixin.qq.com/s/slug'
        links = extract_wechat_links(
            [
                {'type': 'paragraph', 'text': f'**1、[Title]({LONG})**'},
                {'type': 'paragraph', 'text': f'{LONG}。 {short}，'},
                {'type': 'link', 'text': 'Title', 'href': short},
                {'type': 'image', 'href': short + '?scene=2'},
                {'type': 'code', 'text': 'https://mp.weixin.qq.com/s/code'},
                {'type': 'paragraph', 'text': '`https://mp.weixin.qq.com/s/inline`'},
                {'type': 'paragraph', 'text': 'A title without any URL'},
            ]
        )
        self.assertEqual([LONG, short, short + '?scene=2'], [link.url for link in links])
        self.assertEqual(2, len({link.identity for link in links}))
