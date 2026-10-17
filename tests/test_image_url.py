"""Scraped image URLs are repaired once, at ingest and again at fetch.

Article HTML is untrusted. Two shapes of damage reached the database: a WeChat
address whose ``?`` was lost (``.../640&wxfrom=5``, which the origin rejects with
a 400), and a trailing label glued onto the address (``.../640?wx_fmt=jpeg "名.png"``).
Both turned into a broken picture in the list and the reader, so the repair is a
small pure function the write path and the fetch path share.
"""

import unittest

from hippo.utils import sanitize_image_url


class SanitizeImageUrlTest(unittest.TestCase):
    def test_restores_the_lost_query_marker(self) -> None:
        self.assertEqual(
            sanitize_image_url('http://mmbiz.qpic.cn/mmbiz/NtOP/640&wxfrom=5'),
            'http://mmbiz.qpic.cn/mmbiz/NtOP/640?wxfrom=5',
        )

    def test_drops_a_trailing_space_separated_label(self) -> None:
        self.assertEqual(
            sanitize_image_url('https://mmbiz.qpic.cn/a/640?wx_fmt=jpeg "张迪雅  (2'),
            'https://mmbiz.qpic.cn/a/640?wx_fmt=jpeg',
        )

    def test_drops_a_trailing_quoted_name_without_a_space(self) -> None:
        self.assertEqual(
            sanitize_image_url('https://mmbiz.qpic.cn/a/640?wx_fmt=png"1.png"'),
            'https://mmbiz.qpic.cn/a/640?wx_fmt=png',
        )

    def test_keeps_a_well_formed_url_with_a_query_untouched(self) -> None:
        url = 'https://i.ytimg.com/vi/x/hq720.jpg?sqp=abc&rs=def'
        self.assertEqual(sanitize_image_url(url), url)

    def test_keeps_a_query_that_already_has_a_question_mark(self) -> None:
        # The ``/640&`` repair must not fire when a query is already present.
        url = 'https://mmbiz.qpic.cn/a/640?wx_fmt=png&wxfrom=5'
        self.assertEqual(sanitize_image_url(url), url)

    def test_rejects_a_value_that_is_not_an_http_address(self) -> None:
        for value in (r'C:\Users\eric\x.jpg', '', '   ', None, 5, 'data:image/png;base64,AAAA'):
            self.assertIsNone(sanitize_image_url(value), value)

    def test_strips_a_leading_space_before_http(self) -> None:
        self.assertEqual(
            sanitize_image_url(' http://statics.xiumi.us/a.png'),
            'http://statics.xiumi.us/a.png',
        )


if __name__ == '__main__':
    unittest.main()
