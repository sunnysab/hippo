"""Resolve only the links actually present in an authorized source article."""

from __future__ import annotations

import json
from typing import Any

from .article_queries import _assert_article_visible
from .exceptions import ApiError
from .storage import PostgresStorage, fetchone_row
from .wechat_links import extract_wechat_links, parse_wechat_link

MAX_REFERENCE_LINKS = 200


async def article_references(
    storage: PostgresStorage,
    user_id: int,
    article_id: int,
    *,
    enqueue: bool = False,
    retry_url: str | None = None,
) -> dict[str, Any]:
    await _assert_article_visible(storage, user_id, article_id)
    source = await fetchone_row(
        storage,
        """
        SELECT c.content_json,
               EXISTS (SELECT 1 FROM subscription s WHERE s.biz = a.biz
                       AND s.user_id = %s AND NOT s.is_disabled) AS followed
          FROM articles a JOIN article_content c ON c.article_pk = a.id
         WHERE a.id = %s
        """
        + (' FOR SHARE OF c' if enqueue else ''),
        [user_id, article_id],
    )
    # Opening a referenced, unfollowed publisher must not recursively crawl its links.
    if not source or not source['followed']:
        return {'items': []}
    blocks = source['content_json']
    if isinstance(blocks, str):
        try:
            blocks = json.loads(blocks)
        except ValueError:
            blocks = None
    links = extract_wechat_links(blocks) if isinstance(blocks, list) else []
    if len(links) > MAX_REFERENCE_LINKS:
        raise ApiError('Too many article references', status=422)
    selected = parse_wechat_link(retry_url) if retry_url else None
    if retry_url and (selected is None or selected.url not in {link.url for link in links}):
        raise ApiError('URL is not a reference of this article', status=400)
    targets = await storage.references.resolve(links)
    states = await storage.references.states(links)
    if enqueue:
        await storage.references.register(article_id, links, targets)
        queued: set[str] = set()
        for link in sorted(links, key=lambda link: link.identity):
            target = targets.get(link.identity)
            if (target and target['ready']) or link.identity in queued:
                continue
            if selected and selected.identity != link.identity:
                continue
            await storage.references.enqueue(link, article_pk=article_id, retry=selected is not None)
            queued.add(link.identity)
        # A worker can finish between lookup and registration; publish the new body and grant together.
        refreshed = await storage.references.resolve(links)
        if refreshed != targets:
            await storage.references.register(article_id, links, refreshed)
            targets = refreshed
        states = await storage.references.states(links)
    items = []
    for link in links:
        target = targets.get(link.identity)
        state = (states.get(link.identity) or {}).get('state')
        ready = bool(target and target['ready'])
        items.append(
            {
                'url': link.url,
                'status': 'ready' if ready else state if state in {'pending', 'processing', 'failed'} else 'missing',
                'article_id': target['id'] if ready else None,
                'title': target['title'] if target else None,
                'canonical_url': target['link'] if target else None,
            }
        )
    return {'items': items}
