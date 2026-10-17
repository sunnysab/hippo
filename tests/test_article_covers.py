"""Cover selection for the list, the reader's player poster, and the image route.

Three related contracts:

* ``_ensure_cover_image`` registers a cover as ``article_images.kind='cover'``
  before its bytes are archived, so the list must still hand out its id — but a
  candidate whose known hash is globally blocked never is. The card points at
  ``/api/image/{id}`` (protected, on-demand origin fetch), never at a CDN URL,
  and an unarchived *body* image must not become a thumbnail.
* A video/audio block carries the provider's poster URL — the same cover at
  another resolution or with a fresh signature. It must never reach the browser
  as an external URL when we have (or should have) a canonical cover, and a
  blocked/missing cover means no poster at all, not a remote fallback. The stored
  ``article_content.content_json`` is never edited.
* The image route itself refuses a blocked image with 404, after the ownership
  check, whether or not the hash was known before the payload was fetched.
"""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from hippo import article_queries
from hippo.article_queries import (
    ARTICLE_THUMB_IMAGE_SQL,
    _build_article_query,
    _cover_image_id,
    _fetch_image,
    _get_article,
)
from hippo.exceptions import ApiError


def _list_query() -> tuple[str, list[object]]:
    return _build_article_query(
        storage=SimpleNamespace(),
        user_id=42,
        group_ids=None,
        biz=None,
        item_show_type=None,
        query=None,
        exclude_keywords=None,
        since_ts=None,
        until_ts=None,
        sort_mode='publish_at_desc',
        limit=20,
        offset=0,
    )


class ThumbnailCandidateSqlTest(unittest.TestCase):
    """The list query chooses one image per article — pinned as SQL text, so a
    fake/unit test proves the predicate, not a real Postgres run."""

    def test_an_unarchived_cover_is_a_candidate(self) -> None:
        self.assertIn("i.kind = 'cover'", ARTICLE_THUMB_IMAGE_SQL)
        self.assertIn('i.orig_url IS NOT NULL', ARTICLE_THUMB_IMAGE_SQL)
        self.assertIn("i.orig_url <> ''", ARTICLE_THUMB_IMAGE_SQL)

    def test_an_unarchived_body_image_is_not_a_candidate(self) -> None:
        # An image qualifies either because its bytes are archived or because it
        # is this article's cover; the cover clause is kind-constrained on purpose.
        self.assertIn("OR (i.kind = 'cover' AND i.orig_url", ARTICLE_THUMB_IMAGE_SQL)
        self.assertIn("AND ((i.s3_key IS NOT NULL AND i.s3_key <> '')", ARTICLE_THUMB_IMAGE_SQL)

    def test_a_blocked_candidate_is_never_handed_out(self) -> None:
        # Same global semantics the detail view uses: anything whose known hash
        # is on the block list is not a thumbnail candidate.
        self.assertIn('blocked_image_hashes', ARTICLE_THUMB_IMAGE_SQL)
        self.assertIn('NOT EXISTS', ARTICLE_THUMB_IMAGE_SQL)
        self.assertIn('b.hash_algo = i.hash_algo', ARTICLE_THUMB_IMAGE_SQL)
        self.assertIn('b.content_hash = i.content_hash', ARTICLE_THUMB_IMAGE_SQL)

    def test_a_cover_wins_and_an_archived_image_is_next(self) -> None:
        order = ARTICLE_THUMB_IMAGE_SQL.split('ORDER BY', 1)[1]
        self.assertLess(order.index("(i.kind = 'cover')"), order.index('(i.s3_key IS NOT NULL'))
        self.assertIn('i.position ASC', order)

    def test_the_list_still_selects_the_image_id(self) -> None:
        sql, _params = _list_query()

        self.assertIn(ARTICLE_THUMB_IMAGE_SQL, sql)
        self.assertIn('img.id AS image_id', sql)

    def test_the_query_parameters_are_unchanged(self) -> None:
        _sql, params = _list_query()

        # subscription, read state, star state, then LIMIT/OFFSET: the image
        # choice is a join, not a filter, so it binds nothing.
        self.assertEqual([42, 42, 42, 20, 0], params)


class CoverImageIdTest(unittest.TestCase):
    def test_the_column_holds_an_id(self) -> None:
        self.assertEqual(12, _cover_image_id(12))
        self.assertEqual(12, _cover_image_id('12'))

    def test_a_legacy_url_or_an_empty_value_is_not_an_id(self) -> None:
        self.assertIsNone(_cover_image_id(None))
        self.assertIsNone(_cover_image_id(''))
        self.assertIsNone(_cover_image_id('  '))
        self.assertIsNone(_cover_image_id('https://mmbiz.qpic.cn/cover.jpg'))

    def test_exotic_metadata_is_not_an_id_instead_of_an_error(self) -> None:
        # `'²'.isdigit()` is true but `int('²')` raises: a cover column value we
        # did not write must not turn a response into a 500.
        for value in ('²', '١٢', '-3', '0', '1.5', '12a', True, False):
            self.assertIsNone(_cover_image_id(value), value)


class _FakeImages:
    def __init__(self, images: list[dict], blocked: tuple[int, ...] = ()) -> None:
        self._images = images
        self._blocked = set(blocked)

    async def get_article_images(self, article_pk: int) -> list[dict]:
        return [dict(image) for image in self._images]

    async def has_blocked_hashes(self) -> bool:
        return False

    async def list_blocked_image_ids(self, article_pk: int) -> set[int]:
        return set(self._blocked)


class _FakeStorage(SimpleNamespace):
    def __init__(self, images: list[dict], blocked: tuple[int, ...] = ()) -> None:
        super().__init__(
            images=_FakeImages(images, blocked),
            media_jobs=SimpleNamespace(states_for_articles=AsyncMock(return_value={})),
            # Production PostgresStorage always exposes media_assets; these tests
            # are about covers, so no archived video is found.
            media_assets=SimpleNamespace(get=AsyncMock(return_value=None)),
        )


COVER_URL = 'https://i0.hdslb.com/bfs/archive/cover.jpg'
VIDEO_BLOCK = {
    'type': 'video',
    'provider': 'bilibili',
    'embed_url': 'https://player.bilibili.com/player.html?bvid=BV1',
    'cover_url': COVER_URL,
    'video_id': 'BV1',
}


def _article_row(cover: object) -> dict[str, object]:
    return {
        'id': 1,
        'biz': 'biz_1',
        'article_id': 'a1',
        'title': 'Title',
        'item_show_type': 5,
        'author': None,
        'digest': None,
        'cover': cover,
        'link': 'https://example.test/a1',
        'source_url': 'https://example.test/a1',
        'publish_at': None,
        'duration_seconds': None,
        'created_at': None,
        'source': 'bilibili',
        'starred_at': None,
        'read_at': None,
        'account_nickname': 'Channel',
        'account_alias': None,
        'account_avatar': None,
        'group_id': 1,
        'group_name': 'Default',
    }


class ContentPosterTest(unittest.IsolatedAsyncioTestCase):
    """``_get_article`` maps posters onto the protected route, or drops them."""

    def _storage(self, images: list[dict], blocked: tuple[int, ...] = ()) -> _FakeStorage:
        return _FakeStorage(images, blocked)

    def _cover_images(self, **overrides: object) -> list[dict]:
        return [{'id': 9, 'position': 0, 'kind': 'cover', 'content_hash': 'h', **overrides}]

    async def _get(self, storage: _FakeStorage, content: list[dict], cover: object = 9) -> dict:
        rows = [
            _article_row(cover),
            {'content_json': content, 'updated_at': '2026-05-02T11:00:00+00:00'},
        ]
        with patch.object(article_queries, 'fetchone_row', AsyncMock(side_effect=rows)):
            return await _get_article(storage, 7, 1)

    async def test_the_matching_poster_goes_through_the_protected_route(self) -> None:
        storage = self._storage(self._cover_images())

        result = await self._get(storage, [dict(VIDEO_BLOCK)])

        block = result['content'][0]
        self.assertEqual('/api/image/9', block['cover_url'])
        # The rest of the block is untouched.
        self.assertEqual(VIDEO_BLOCK['embed_url'], block['embed_url'])
        self.assertEqual('BV1', block['video_id'])

    async def test_another_resolution_of_the_same_cover_is_canonicalised(self) -> None:
        # The list and the detail view may carry different URLs for one cover
        # (YouTube sizes, Bilibili signatures): identity is the article's cover
        # id, not URL equality with the stored ``orig_url``.
        poster = 'https://i.ytimg.com/vi/BV1/hqdefault.jpg'
        block = {
            'type': 'video',
            'provider': 'youtube',
            'embed_url': 'https://www.youtube.com/embed/BV1',
            'cover_url': poster,
            'video_id': 'BV1',
        }
        storage = self._storage(self._cover_images())

        result = await self._get(storage, [block])

        self.assertEqual('/api/image/9', result['content'][0]['cover_url'])

    async def test_the_stored_content_is_not_edited_in_place(self) -> None:
        stored = [dict(VIDEO_BLOCK)]
        storage = self._storage(self._cover_images())

        result = await self._get(storage, stored)

        self.assertEqual(COVER_URL, stored[0]['cover_url'])
        self.assertIsNot(stored[0], result['content'][0])

    async def test_an_already_proxied_poster_is_kept(self) -> None:
        block = {**VIDEO_BLOCK, 'cover_url': '/api/image/9'}
        storage = self._storage(self._cover_images())

        result = await self._get(storage, [dict(block)])

        self.assertEqual('/api/image/9', result['content'][0]['cover_url'])

    async def test_a_blocked_cover_drops_the_external_poster(self) -> None:
        # Keeping the provider URL here would send the reader's browser to an
        # image we refused to serve — the bypass this fix closes.
        storage = self._storage([], blocked=(9,))

        result = await self._get(storage, [dict(VIDEO_BLOCK)])

        self.assertNotIn('cover_url', result['content'][0])
        self.assertEqual(VIDEO_BLOCK['embed_url'], result['content'][0]['embed_url'])

    async def test_a_blocked_cover_also_drops_a_hippo_poster(self) -> None:
        block = {**VIDEO_BLOCK, 'cover_url': '/api/image/9'}
        storage = self._storage([], blocked=(9,))

        result = await self._get(storage, [dict(block)])

        self.assertNotIn('cover_url', result['content'][0])

    async def test_an_article_without_a_cover_keeps_no_external_poster(self) -> None:
        storage = self._storage([{'id': 9, 'position': 0, 'kind': 'cover', 'content_hash': 'h'}])

        result = await self._get(storage, [dict(VIDEO_BLOCK)], cover=None)

        self.assertNotIn('cover_url', result['content'][0])

    async def test_a_legacy_url_cover_is_not_treated_as_an_id(self) -> None:
        storage = self._storage([{'id': 9, 'position': 0, 'kind': 'cover', 'content_hash': 'h'}])

        result = await self._get(storage, [dict(VIDEO_BLOCK)], cover='https://mmbiz.qpic.cn/cover.jpg')

        self.assertNotIn('cover_url', result['content'][0])

    async def test_a_block_without_a_poster_stays_without_one(self) -> None:
        # Nothing to protect, so no poster is invented either.
        block = {'type': 'video', 'provider': 'bilibili', 'embed_url': 'https://example.test/e'}
        storage = self._storage(self._cover_images())

        result = await self._get(storage, [dict(block)])

        self.assertNotIn('cover_url', result['content'][0])

    async def test_a_filtered_body_image_is_not_reintroduced(self) -> None:
        content = [
            {'type': 'image', 'image_id': 4, 'orig_url': 'https://mmbiz.qpic.cn/blocked.jpg'},
            dict(VIDEO_BLOCK),
        ]
        storage = self._storage(
            [
                {'id': 4, 'position': 1, 'kind': 'inline', 'content_hash': 'blocked'},
                {'id': 9, 'position': 0, 'kind': 'cover', 'content_hash': 'h'},
            ],
            blocked=(4,),
        )

        result = await self._get(storage, content)

        self.assertEqual([dict(VIDEO_BLOCK, cover_url='/api/image/9')], result['content'])

    async def test_an_audio_poster_is_proxied_the_same_way(self) -> None:
        audio = {'type': 'audio', 'url': '/api/media/1/audio', 'cover_url': COVER_URL}
        storage = self._storage(self._cover_images())

        result = await self._get(storage, [audio])

        self.assertEqual('/api/image/9', result['content'][0]['cover_url'])

    async def test_a_bytesless_response_is_not_a_500(self) -> None:
        # Nothing in the content matched, and the content is handed back as-is.
        content = [{'type': 'paragraph', 'text': 'hi'}]
        storage = self._storage([], blocked=(9,))

        result = await self._get(storage, content)

        self.assertEqual(content, result['content'])


class FetchImageBlockedTest(unittest.IsolatedAsyncioTestCase):
    """The image route is the second gate, not just the detail view's filter."""

    def _patches(self, rows: list[object], *, visible_error: ApiError | None = None):
        check = AsyncMock(side_effect=visible_error) if visible_error else AsyncMock()
        fetch = AsyncMock(return_value=(b'image-bytes', 'image/png'))
        return (
            check,
            fetch,
            (
                patch.object(article_queries, '_ensure_image_visible', check),
                patch.object(article_queries, 'fetchone_row', AsyncMock(side_effect=rows)),
                patch.object(article_queries, 'fetch_image_bytes', fetch),
            ),
        )

    async def test_an_unauthorized_image_is_refused_before_the_block_check(self) -> None:
        check, fetch, patches = self._patches([], visible_error=ApiError('Image not found', status=404))

        with patches[0], patches[1], patches[2], self.assertRaises(ApiError) as raised:
            await _fetch_image(SimpleNamespace(), 7, 9)

        self.assertEqual(404, raised.exception.status)
        self.assertEqual('Image not found', str(raised.exception))
        check.assert_awaited_once()
        fetch.assert_not_awaited()

    async def test_a_known_blocked_hash_never_reaches_the_bytes(self) -> None:
        rows = [
            {'hash_algo': 'sha256', 'content_hash': 'blocked-hash'},
            {'blocked': 1},
        ]
        check, fetch, patches = self._patches(rows)

        with patches[0], patches[1], patches[2], self.assertRaises(ApiError) as raised:
            await _fetch_image(SimpleNamespace(), 7, 9)

        self.assertEqual(404, raised.exception.status)
        # A blocked image is indistinguishable from one the user may not see.
        self.assertEqual('Image not found', str(raised.exception))
        check.assert_awaited_once()
        fetch.assert_not_awaited()

    async def test_an_unblocked_image_is_served(self) -> None:
        rows = [
            {'hash_algo': 'sha256', 'content_hash': 'good-hash'},
            None,
        ]
        _check, fetch, patches = self._patches(rows)

        with patches[0], patches[1], patches[2]:
            result = await _fetch_image(SimpleNamespace(), 7, 9)

        fetch.assert_awaited_once()
        self.assertEqual((b'image-bytes', 'image/png'), result)

    async def test_an_unknown_hash_is_checked_from_the_bytes_we_already_sent(self) -> None:
        rows = [
            {'hash_algo': None, 'content_hash': None},
            {'blocked': 1},
        ]
        _check, fetch, patches = self._patches(rows)

        with patches[0], patches[1], patches[2], self.assertRaises(ApiError) as raised:
            await _fetch_image(SimpleNamespace(), 7, 9)

        self.assertEqual('Image not found', str(raised.exception))
        # One fetch, not two: the payload is hashed, not downloaded again.
        fetch.assert_awaited_once()

    async def test_an_unknown_unblocked_hash_is_served(self) -> None:
        rows = [
            {'hash_algo': None, 'content_hash': None},
            None,
        ]
        _check, _fetch, patches = self._patches(rows)

        with patches[0], patches[1], patches[2]:
            result = await _fetch_image(SimpleNamespace(), 7, 9)

        self.assertEqual(b'image-bytes', result[0])


if __name__ == '__main__':
    unittest.main()
