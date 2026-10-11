"""What kind of thing an article is, across platforms.

The point of the derivation is that the list badge stops saying "视频分享" about a
Bilibili video: the WeChat layout type is meaningless outside WeChat.
"""

import unittest

from hippo.article_queries import CONTENT_KIND_BY_ITEM_SHOW_TYPE, content_kind


class ContentKindTest(unittest.TestCase):
    def test_weixin_layout_types_collapse_into_kinds(self) -> None:
        self.assertEqual('article', content_kind('weixin', 0))
        self.assertEqual('video', content_kind('weixin', 5))
        self.assertEqual('audio', content_kind('weixin', 7))
        self.assertEqual('gallery', content_kind('weixin', 8))
        self.assertEqual('short', content_kind('weixin', 17))

    def test_an_unknown_weixin_layout_type_is_a_plain_article(self) -> None:
        self.assertEqual('article', content_kind('weixin', 999))
        self.assertEqual('article', content_kind('weixin', None))

    def test_media_sources_ignore_the_weixin_layout_type(self) -> None:
        # 一个 B站视频可能带着微信的 item_show_type=5（历史数据），但类型由来源决定。
        self.assertEqual('video', content_kind('bilibili', 5))
        self.assertEqual('video', content_kind('youtube', None))
        self.assertEqual('audio', content_kind('podcast', 0))

    def test_an_unregistered_source_degrades_to_an_article(self) -> None:
        self.assertEqual('article', content_kind('mastodon', 5))

    def test_a_row_without_a_source_is_treated_as_weixin(self) -> None:
        # 源列出现之前的数据没有 source 字段。
        self.assertEqual('video', content_kind(None, 5))

    def test_every_mapped_kind_is_one_the_frontend_knows(self) -> None:
        expected = {'article', 'video', 'audio', 'gallery', 'text', 'short'}

        self.assertLessEqual(set(CONTENT_KIND_BY_ITEM_SHOW_TYPE.values()), expected)


if __name__ == '__main__':
    unittest.main()
