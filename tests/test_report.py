"""Daily report: day boundaries, grouping and rendering.

Rendering is a pure function of a fixed dataset, which is exactly what makes
the e-mail and the preview agree.
"""

import unittest
from datetime import UTC, date, datetime

from hippo.report.query import day_bounds, local_date, parse_date, resolve_timezone
from hippo.report.render import build_context, group_articles, render_html


def _article(**overrides) -> dict:
    base = {
        'id': 1,
        'title': '一篇文章',
        'digest': '摘要内容',
        'link': 'https://mp.weixin.qq.com/s/abc',
        'author': '作者',
        'publish_at': 1772294400,
        'biz': 'gh_1',
        'nickname': '某公众号',
        'group_id': None,
        'group_name': None,
    }
    base.update(overrides)
    return base


class DayBoundsTest(unittest.TestCase):
    def test_the_window_is_the_user_local_day(self) -> None:
        start, end = day_bounds(date(2026, 3, 1), 'Asia/Shanghai')
        # A day is 24h, and the boundary is local midnight.
        self.assertEqual(86400, end - start)
        local_start = datetime.fromtimestamp(start, tz=UTC).astimezone(resolve_timezone('Asia/Shanghai'))
        self.assertEqual(0, local_start.hour)
        self.assertEqual(1, local_start.day)

    def test_the_same_instant_is_a_different_date_in_another_zone(self) -> None:
        # 23:30 UTC is already the next day in Shanghai.
        moment = datetime(2026, 3, 1, 23, 30, tzinfo=UTC)
        self.assertEqual(date(2026, 3, 1), local_date(moment, 'UTC'))
        self.assertEqual(date(2026, 3, 2), local_date(moment, 'Asia/Shanghai'))

    def test_an_unknown_zone_falls_back_instead_of_raising(self) -> None:
        start, end = day_bounds(date(2026, 3, 1), 'Not/AZone')
        self.assertEqual(86400, end - start)

    def test_a_bad_date_parameter_falls_back_to_today(self) -> None:
        self.assertEqual(date(2026, 3, 5), parse_date('2026-03-05', 'UTC'))
        # Unparseable input resolves to the user's today rather than erroring.
        self.assertIsInstance(parse_date('not-a-date', 'UTC'), date)


class GroupingTest(unittest.TestCase):
    def test_articles_are_bucketed_by_group(self) -> None:
        groups = group_articles([
            _article(id=1, group_id=1, group_name='技术'),
            _article(id=2, group_id=2, group_name='产品'),
            _article(id=3, group_id=1, group_name='技术'),
        ])

        self.assertEqual(['技术', '产品'], [g['name'] for g in groups])
        self.assertEqual(2, len(groups[0]['articles']))

    def test_ungrouped_articles_come_last(self) -> None:
        groups = group_articles([
            _article(id=1, group_id=None),
            _article(id=2, group_id=5, group_name='技术'),
        ])

        self.assertEqual([5, None], [g['id'] for g in groups])
        self.assertEqual('未分组', groups[-1]['name'])

    def test_order_within_a_group_is_preserved(self) -> None:
        groups = group_articles([
            _article(id=1, group_id=1, group_name='g'),
            _article(id=2, group_id=1, group_name='g'),
        ])
        self.assertEqual([1, 2], [a['id'] for a in groups[0]['articles']])


class RenderTest(unittest.TestCase):
    def test_the_render_includes_every_article(self) -> None:
        html = render_html(
            username='admin',
            report_date=date(2026, 3, 1),
            timezone_name='Asia/Shanghai',
            articles=[_article(title='第一篇'), _article(id=2, title='第二篇')],
            site_name='Hippo',
        )

        self.assertIn('第一篇', html)
        self.assertIn('第二篇', html)
        self.assertIn('2026-03-01', html)

    def test_the_group_name_is_rendered_as_a_heading(self) -> None:
        html = render_html(
            username='u',
            report_date=date(2026, 3, 1),
            timezone_name='UTC',
            articles=[_article(group_id=1, group_name='技术')],
        )
        self.assertIn('技术', html)

    def test_an_empty_day_renders_an_explicit_message(self) -> None:
        html = render_html(
            username='u',
            report_date=date(2026, 3, 1),
            timezone_name='UTC',
            articles=[],
        )
        self.assertIn('没有新文章', html)
        self.assertIn('共 0 篇', html)

    def test_titles_are_escaped(self) -> None:
        html = render_html(
            username='u',
            report_date=date(2026, 3, 1),
            timezone_name='UTC',
            articles=[_article(title='<script>alert(1)</script>')],
        )
        self.assertNotIn('<script>alert(1)</script>', html)
        self.assertIn('&lt;script&gt;', html)

    def test_the_digest_is_included_when_present(self) -> None:
        html = render_html(
            username='u',
            report_date=date(2026, 3, 1),
            timezone_name='UTC',
            articles=[_article(digest='这段摘要应该出现')],
        )
        self.assertIn('这段摘要应该出现', html)

    def test_the_publish_time_is_localized(self) -> None:
        context = build_context(
            username='u',
            report_date=date(2026, 3, 1),
            timezone_name='Asia/Shanghai',
            articles=[_article(publish_at=1772294400)],
        )
        rendered = context['groups'][0]['articles'][0]['display_time']
        # Shanghai is UTC+8.
        expected = datetime.fromtimestamp(1772294400, tz=UTC).astimezone(
            resolve_timezone('Asia/Shanghai')
        ).strftime('%H:%M')
        self.assertEqual(expected, rendered)


if __name__ == '__main__':
    unittest.main()
