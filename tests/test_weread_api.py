import unittest

from hippo.models import ArticleRecord
from hippo.wechat_api import (
    biz_to_book_id,
    book_id_to_biz,
    build_user_agent,
    native_signature,
    parse_mp_chapters,
    wechat_article_url,
)


class WereadPrimitivesTest(unittest.TestCase):
    def test_build_user_agent_default(self) -> None:
        self.assertEqual(
            build_user_agent(),
            'WeRead/10.2.1 WRBrand/other Android WeRead',
        )

    def test_native_signature_matches_reference_vector(self) -> None:
        # Pinned vector from weread_mp/test_cli.py (Android 10.2.1 APK).
        self.assertEqual(
            native_signature(['abc', '123', 'xyz']),
            '80116e3df7ccce221a0ca8edfe2e2048fad5902cc3f3d45289c907dc30743acd',
        )

    def test_wechat_article_url(self) -> None:
        self.assertEqual(
            wechat_article_url(
                'MP_WXS_3075193430',
                'MP_WXS_3075193430_YTOsfKkLbp5bX5e9t0I7ZA',
            ),
            'https://mp.weixin.qq.com/s/YTOsfKkLbp5bX5e9t0I7ZA',
        )

    def test_wechat_article_url_rejects_mismatched_prefix(self) -> None:
        self.assertIsNone(
            wechat_article_url('MP_WXS_3075193430', 'OTHER_REVIEW_ID'),
        )


class BizBookIdConversionTest(unittest.TestCase):
    def test_book_id_to_biz_known(self) -> None:
        self.assertEqual(
            book_id_to_biz('MP_WXS_3075193430'),
            'MzA3NTE5MzQzMA==',
        )

    def test_biz_to_book_id_known_with_padding(self) -> None:
        self.assertEqual(
            biz_to_book_id('MzA3NTE5MzQzMA=='),
            'MP_WXS_3075193430',
        )

    def test_biz_to_book_id_known_without_padding(self) -> None:
        self.assertEqual(
            biz_to_book_id('MzA3NTE5MzQzMA'),
            'MP_WXS_3075193430',
        )

    def test_round_trip_via_book_id(self) -> None:
        for book_id in ('MP_WXS_3075193430', 'MP_WXS_1', 'MP_WXS_42'):
            self.assertEqual(biz_to_book_id(book_id_to_biz(book_id)), book_id)

    def test_round_trip_via_biz(self) -> None:
        for biz in ('MzA3NTE5MzQzMA==', 'MzE=', 'NDI='):
            self.assertEqual(book_id_to_biz(biz_to_book_id(biz)), biz)

    def test_biz_to_book_id_idempotent(self) -> None:
        self.assertEqual(
            biz_to_book_id('MP_WXS_3075193430'),
            'MP_WXS_3075193430',
        )

    def test_book_id_to_biz_passes_through_non_weread(self) -> None:
        self.assertEqual(book_id_to_biz('gh_abc'), 'gh_abc')


class ParseMpChaptersTest(unittest.TestCase):
    BIZ = 'MzA3NTE5MzQzMA=='  # MP_WXS_3075193430

    def test_parses_review_items_into_records(self) -> None:
        payload = {
            'data': [
                {
                    'reviewId': 'MP_WXS_3075193430_YTOsfKkLbp5bX5e9t0I7ZA',
                    'createTime': 1700000000,
                    'mpInfo': {'title': 'Test Article', 'pic_url': 'https://mmbiz.qpic.cn/cover.jpg'},
                },
                {'reviewId': 'MP_WXS_3075193430_notime', 'mpInfo': {'title': 'No Time'}},
                {'reviewId': 'OTHER_PREFIX'},
                {},
            ],
            'clearAll': 0,
            'synckey': 'abc',
        }
        records = parse_mp_chapters(self.BIZ, payload)

        self.assertEqual(len(records), 2)
        first: ArticleRecord = records[0]
        self.assertEqual(first.biz, self.BIZ)
        self.assertEqual(first.article_id, 'MP_WXS_3075193430_YTOsfKkLbp5bX5e9t0I7ZA')
        self.assertEqual(first.link, 'https://mp.weixin.qq.com/s/YTOsfKkLbp5bX5e9t0I7ZA')
        self.assertEqual(first.title, 'Test Article')
        self.assertEqual(first.publish_at, 1700000000)
        self.assertEqual(first.cover, 'https://mmbiz.qpic.cn/cover.jpg')
        self.assertIsNone(first.item_show_type)
        self.assertIsNone(first.author)
        self.assertIsNone(records[1].publish_at)

    def test_missing_data_returns_empty(self) -> None:
        self.assertEqual(parse_mp_chapters(self.BIZ, {}), [])
        self.assertEqual(parse_mp_chapters(self.BIZ, {'data': 'oops'}), [])


if __name__ == '__main__':
    unittest.main()
