"""Report rendering.

One template serves both the e-mail and the web preview, so what an admin
previews is exactly what the reader receives. Styles are inline because mail
clients strip <style> blocks.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader

_TEMPLATE_DIR = Path(__file__).resolve().parent.parent / 'templates'

# autoescape=True, not select_autoescape: the latter keys off the file
# extension, and these templates end in .j2, so it would leave every article
# title — which comes from WeChat, i.e. outside input — unescaped.
_environment = Environment(
    loader=FileSystemLoader(str(_TEMPLATE_DIR)),
    autoescape=True,
    trim_blocks=True,
    lstrip_blocks=True,
)


def _format_time(publish_at: Any, timezone_name: str) -> str:
    """Local publication time, so the report reads like the reader's day."""
    from .query import resolve_timezone

    if not publish_at:
        return ''
    try:
        moment = datetime.fromtimestamp(int(publish_at), tz=UTC)
    except TypeError, ValueError, OSError:
        return ''
    return moment.astimezone(resolve_timezone(timezone_name)).strftime('%H:%M')


def group_articles(articles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Bucket articles by group, preserving the incoming (newest-first) order.

    Ungrouped subscriptions land in a trailing bucket rather than being dropped.
    """
    buckets: dict[int | None, dict[str, Any]] = {}
    for article in articles:
        key = article.get('group_id')
        bucket = buckets.get(key)
        if bucket is None:
            bucket = {
                'id': key,
                'name': article.get('group_name') or '未分组',
                'articles': [],
            }
            buckets[key] = bucket
        bucket['articles'].append(article)
    # None last: the ungrouped bucket belongs at the end.
    return sorted(buckets.values(), key=lambda item: (item['id'] is None, item['id'] or 0))


def build_context(
    *,
    username: str,
    report_date: date,
    timezone_name: str,
    articles: list[dict[str, Any]],
    site_name: str = 'Hippo',
    public_base_url: str = '',
) -> dict[str, Any]:
    """Assemble the template context.

    Pure: same inputs produce the same HTML, which is what makes the rendered
    output testable against a fixed fixture.
    """
    grouped = group_articles(articles)
    base = public_base_url.rstrip('/')
    for bucket in grouped:
        for article in bucket['articles']:
            article['display_time'] = _format_time(article.get('publish_at'), timezone_name)
            article['display_url'] = f'{base}/#/articles' if base else ''
    return {
        'site_name': site_name,
        'username': username,
        'report_date': report_date.isoformat(),
        'timezone': timezone_name,
        'groups': grouped,
        'total': len(articles),
        'base_url': base,
    }


def render_report(context: dict[str, Any]) -> str:
    """Render the report to a self-contained HTML document."""
    return _environment.get_template('report.html.j2').render(**context)


def render_html(
    *,
    username: str,
    report_date: date,
    timezone_name: str,
    articles: list[dict[str, Any]],
    site_name: str = 'Hippo',
    public_base_url: str = '',
) -> str:
    """Convenience wrapper around :func:`build_context` and :func:`render_report`."""
    return render_report(
        build_context(
            username=username,
            report_date=report_date,
            timezone_name=timezone_name,
            articles=articles,
            site_name=site_name,
            public_base_url=public_base_url,
        )
    )


__all__ = ['build_context', 'group_articles', 'render_html', 'render_report']
