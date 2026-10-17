"""The delivery contract for archived media, from the JSON payload to the bytes.

Three related contracts:

* ``_get_article`` hydrates an article's own stored video into a native block —
  ``url=/api/media/{id}/video`` and no ``embed_url`` — so a reader can play what
  we archived even while the transcript is still being written. The stored
  ``article_content.content_json`` is never edited, an unstored or keyless archive
  never becomes a player, and an article with no video pays no media lookup.
* ``/api/media/{pk}/video`` serves bytes for a stored archive, a 404 otherwise: a
  video's ``orig_url`` is a third-party page, not a redirect target. Audio keeps
  its original-URL fallback.
* ``parse_range`` answers suffix/open/out-of-bounds requests from the same grammar,
  and the route refuses an impossible range with ``Content-Range: bytes */total``
  instead of inventing a 206.
"""

import copy
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from hippo import article_queries
from hippo.api import deps
from hippo.api.errors import install_exception_handlers
from hippo.api.routers import article as article_api
from hippo.article_queries import _ensure_image_visible, _fetch_image, _get_article
from hippo.exceptions import ApiError
from hippo.media.archive import ArchiveError, AudioStream, parse_range

ARTICLE_PK = 7
COVER_URL = 'https://i0.hdslb.com/bfs/archive/cover.jpg'
STORE_KEY = 'media/video/7.mp4'
VIDEO_URL = f'/api/media/{ARTICLE_PK}/video'

EMBED_VIDEO_BLOCK = {
    'type': 'video',
    'provider': 'bilibili',
    'embed_url': 'https://player.bilibili.com/player.html?bvid=BV1',
    'cover_url': COVER_URL,
    'video_id': 'BV1',
}
PARAGRAPHS = [
    {'type': 'paragraph', 'text': '第一句', 't': 0.0},
    {'type': 'paragraph', 'text': '第二句', 't': 62.5},
]


class _Images:
    def __init__(self, images: list[dict], blocked: tuple[int, ...] = ()) -> None:
        self._images = images
        self._blocked = set(blocked)

    async def get_article_images(self, article_pk: int) -> list[dict]:
        return [dict(image) for image in self._images]

    async def has_blocked_hashes(self) -> bool:
        return False

    async def list_blocked_image_ids(self, article_pk: int) -> set[int]:
        return set(self._blocked)


class _MediaAssets:
    def __init__(self, asset: dict | None) -> None:
        self.get = AsyncMock(return_value=asset)


class _Jobs:
    def __init__(self, state: str | None) -> None:
        self.states_for_articles = AsyncMock(return_value={ARTICLE_PK: state} if state else {})


def _storage(
    *,
    asset: dict | None = None,
    images: list[dict] | None = None,
    blocked: tuple[int, ...] = (),
    job_state: str | None = None,
) -> SimpleNamespace:
    # Production PostgresStorage always exposes media_assets, so every fake does
    # too: a test double that hides an interface would test the wrong thing.
    return SimpleNamespace(
        images=_Images(images or [], blocked),
        media_jobs=_Jobs(job_state),
        media_assets=_MediaAssets(asset),
    )


def _article_row(*, source: str = 'bilibili', cover: object = None, item_show_type: int = 5) -> dict[str, object]:
    return {
        'id': ARTICLE_PK,
        'biz': 'biz_1',
        'article_id': 'a1',
        'title': 'Title',
        'item_show_type': item_show_type,
        'author': None,
        'digest': None,
        'cover': cover,
        'link': 'https://example.test/a1',
        'source_url': 'https://example.test/a1',
        'publish_at': None,
        'duration_seconds': None,
        'created_at': None,
        'source': source,
        'starred_at': None,
        'read_at': None,
        'account_nickname': 'Channel',
        'account_alias': None,
        'account_avatar': None,
        'group_id': 1,
        'group_name': 'Default',
    }


def _cover_images(**overrides: object) -> list[dict]:
    return [{'id': 9, 'position': 0, 'kind': 'cover', 'content_hash': 'h', **overrides}]


def _user() -> SimpleNamespace:
    return SimpleNamespace(id=1, username='u')


class ArchivedVideoHydrationTest(unittest.IsolatedAsyncioTestCase):
    """``_get_article`` hands out a native block for a stored video, or nothing."""

    async def _get(
        self,
        storage: SimpleNamespace,
        content: object,
        *,
        source: str = 'bilibili',
        cover: object = None,
        item_show_type: int = 5,
        attempts: dict | None = None,
    ) -> dict:
        rows: list[dict | None] = [
            _article_row(source=source, cover=cover, item_show_type=item_show_type),
            None if content is ... else {'content_json': content, 'updated_at': '2026-05-02T11:00:00+00:00'},
            attempts,
        ]
        with patch.object(article_queries, 'fetchone_row', AsyncMock(side_effect=rows)):
            return await _get_article(storage, 7, ARTICLE_PK)

    async def test_a_stored_video_plays_without_transcript_content(self) -> None:
        storage = _storage(asset={'state': 'stored', 's3_key': STORE_KEY, 'size_bytes': 2048}, job_state='processing')

        result = await self._get(storage, ...)

        self.assertEqual('ok', result['content_status'])
        self.assertEqual([{'type': 'video', 'url': VIDEO_URL}], result['content'])
        # The job is still running, but this response is not claiming a transcript.
        storage.media_jobs.states_for_articles.assert_not_awaited()

    async def test_an_old_embed_block_is_replaced_where_it_sits(self) -> None:
        stored = [copy.deepcopy(EMBED_VIDEO_BLOCK), *copy.deepcopy(PARAGRAPHS)]
        storage = _storage(asset={'state': 'stored', 's3_key': STORE_KEY}, job_state='pending')

        result = await self._get(storage, stored)

        video, *rest = result['content']
        self.assertNotIn('embed_url', video)
        self.assertEqual(VIDEO_URL, video['url'])
        # Provider metadata survives, and the transcript keeps its anchors.
        self.assertEqual('bilibili', video['provider'])
        self.assertEqual('BV1', video['video_id'])
        self.assertEqual(PARAGRAPHS, rest)
        self.assertEqual('ok', result['content_status'])
        storage.media_jobs.states_for_articles.assert_not_awaited()

    async def test_the_stored_json_is_never_edited(self) -> None:
        stored = [copy.deepcopy(EMBED_VIDEO_BLOCK), *copy.deepcopy(PARAGRAPHS)]
        storage = _storage(asset={'state': 'stored', 's3_key': STORE_KEY})

        result = await self._get(storage, stored)

        self.assertEqual(EMBED_VIDEO_BLOCK, stored[0])
        self.assertNotIn('url', stored[0])
        self.assertIsNot(stored[0], result['content'][0])

    async def test_a_stored_video_without_a_video_block_is_put_in_front(self) -> None:
        stored = copy.deepcopy(PARAGRAPHS)
        storage = _storage(asset={'state': 'stored', 's3_key': STORE_KEY})

        result = await self._get(storage, stored)

        self.assertEqual('video', result['content'][0]['type'])
        self.assertEqual(VIDEO_URL, result['content'][0]['url'])
        self.assertEqual(PARAGRAPHS, result['content'][1:])
        self.assertEqual(PARAGRAPHS, stored)

    async def test_an_unstored_video_does_not_become_a_player(self) -> None:
        storage = _storage(
            asset={'state': 'failed', 's3_key': None, 'orig_url': 'https://www.bilibili.com/video/BV1'},
            job_state='pending',
        )

        result = await self._get(storage, ...)

        self.assertIsNone(result['content'])
        self.assertEqual('processing', result['content_status'])

    async def test_a_keyless_stored_row_does_not_become_a_player(self) -> None:
        storage = _storage(asset={'state': 'stored', 's3_key': '', 'size_bytes': 10})

        result = await self._get(storage, ...)

        self.assertIsNone(result['content'])
        self.assertEqual('missing', result['content_status'])

    async def test_an_audio_entry_is_not_dressed_up_as_video(self) -> None:
        # The stored object behind a podcast is audio: querying it as a video would
        # hand out the wrong bytes.
        storage = _storage(asset={'state': 'stored', 's3_key': 'media/audio/7.mp3'}, job_state='pending')

        result = await self._get(storage, ..., source='podcast')

        self.assertIsNone(result['content'])
        self.assertEqual('processing', result['content_status'])
        storage.media_assets.get.assert_not_awaited()

    async def test_a_plain_article_never_pays_a_media_lookup(self) -> None:
        storage = _storage(job_state='pending')

        result = await self._get(storage, [{'type': 'paragraph', 'text': 'hi'}], source='weixin', item_show_type=0)

        self.assertEqual('ok', result['content_status'])
        storage.media_assets.get.assert_not_awaited()

    async def test_a_video_block_is_canonicalised_to_the_visible_cover(self) -> None:
        storage = _storage(
            asset={'state': 'stored', 's3_key': STORE_KEY},
            images=_cover_images(),
        )

        result = await self._get(storage, ..., cover='9')

        self.assertEqual('/api/image/9', result['content'][0]['cover_url'])

    async def test_a_foreign_blocked_cover_is_not_leaked_as_a_poster(self) -> None:
        storage = _storage(
            asset={'state': 'stored', 's3_key': STORE_KEY},
            images=[],
            blocked=(9,),
        )

        result = await self._get(storage, ..., cover='9')

        self.assertNotIn('cover_url', result['content'][0])
        self.assertEqual(VIDEO_URL, result['content'][0]['url'])

    async def test_a_stored_video_with_a_legacy_url_cover_gets_no_poster(self) -> None:
        storage = _storage(asset={'state': 'stored', 's3_key': STORE_KEY}, images=_cover_images())

        result = await self._get(storage, ..., cover='https://mmbiz.qpic.cn/cover.jpg')

        self.assertNotIn('cover_url', result['content'][0])


def _client(storage: SimpleNamespace) -> TestClient:
    app = FastAPI()
    install_exception_handlers(app)

    async def fake_current_user(request: Request):
        return _user()

    app.dependency_overrides[deps.current_user] = fake_current_user
    app.dependency_overrides[deps.get_storage] = lambda: storage
    app.include_router(article_api.router, prefix='/api')
    return TestClient(app)


class ArchivedVideoRouteTest(unittest.TestCase):
    """The media route over HTTP, with a fake store and no network."""

    def _app(self, storage: SimpleNamespace, *, visible: bool = True) -> TestClient:
        visibility = AsyncMock() if visible else AsyncMock(side_effect=ApiError('Article not found', status=404))
        self.visibility = patch.object(article_api, '_assert_article_visible', visibility)
        self.visibility.start()
        self.addCleanup(self.visibility.stop)
        return _client(storage)

    def test_a_stored_video_answers_a_range_with_206(self) -> None:
        asset = {'state': 'stored', 's3_key': STORE_KEY, 'size_bytes': 100}
        storage = _storage(asset=asset)
        opened = Mock(return_value=AudioStream(body=iter([b'x' * 10]), content_type='video/mp4', size=10, total=100))
        with patch.object(article_api.archive_module, 'open_audio', opened):
            response = self._app(storage).get(f'/api/media/{ARTICLE_PK}/video', headers={'range': 'bytes=0-9'})

        self.assertEqual(206, response.status_code)
        self.assertEqual('bytes 0-9/100', response.headers['content-range'])
        self.assertEqual('10', response.headers['content-length'])
        self.assertEqual('bytes', response.headers['accept-ranges'])
        self.assertEqual('private, max-age=3600', response.headers['cache-control'])
        opened.assert_called_once_with(STORE_KEY, start=0, end=9)
        storage.media_assets.get.assert_awaited_once_with(ARTICLE_PK, kind='video')

    def test_an_impossible_range_is_a_416_with_the_real_length(self) -> None:
        asset = {'state': 'stored', 's3_key': STORE_KEY, 'size_bytes': 100}
        storage = _storage(asset=asset)
        opened = Mock()
        with patch.object(article_api.archive_module, 'open_audio', opened):
            response = self._app(storage).get(f'/api/media/{ARTICLE_PK}/video', headers={'range': 'bytes=500-600'})

        self.assertEqual(416, response.status_code)
        self.assertEqual('bytes */100', response.headers['content-range'])
        self.assertEqual('http_416', response.json()['code'])
        opened.assert_not_called()

    def test_a_video_without_stored_bytes_is_a_404_not_a_redirect(self) -> None:
        asset = {'state': 'failed', 's3_key': None, 'orig_url': 'https://www.bilibili.com/video/BV1'}
        storage = _storage(asset=asset)
        response = self._app(storage).get(f'/api/media/{ARTICLE_PK}/video')

        self.assertEqual(404, response.status_code)
        self.assertNotIn('location', response.headers)

    def test_audio_keeps_its_original_url_fallback(self) -> None:
        asset = {'state': 'failed', 's3_key': None, 'orig_url': 'https://cdn/ep.mp3'}
        storage = _storage(asset=asset)
        response = self._app(storage).get(f'/api/media/{ARTICLE_PK}/audio', follow_redirects=False)

        self.assertEqual(302, response.status_code)
        self.assertEqual('https://cdn/ep.mp3', response.headers['location'])
        self.assertEqual('audio', storage.media_assets.get.await_args.kwargs['kind'])

    def test_a_dangerous_scheme_is_not_redirected_to(self) -> None:
        # ``orig_url`` is provider metadata: a scheme the browser cannot play must
        # not become a redirect, and the reader gets the ordinary "no archive".
        for bad in (
            'javascript:alert(1)',
            'data:audio/mpeg;base64,AAAA',
            'file:///etc/passwd',
            'not a url',
            'https://',
            '',
        ):
            asset = {'state': 'failed', 's3_key': None, 'orig_url': bad}
            response = self._app(_storage(asset=asset)).get(f'/api/media/{ARTICLE_PK}/audio', follow_redirects=False)

            self.assertEqual(404, response.status_code, bad)
            self.assertNotIn('location', response.headers, bad)

    def test_an_audio_fallback_without_a_host_is_refused(self) -> None:
        # ``https:///ep.mp3`` has no hostname, so there is nowhere to send the reader.
        asset = {'state': 'failed', 's3_key': None, 'orig_url': 'https:///ep.mp3'}
        response = self._app(_storage(asset=asset)).get(f'/api/media/{ARTICLE_PK}/audio', follow_redirects=False)

        self.assertEqual(404, response.status_code)
        self.assertNotIn('location', response.headers)

    def test_a_video_source_url_is_never_used_as_a_fallback(self) -> None:
        asset = {'state': 'failed', 's3_key': None, 'orig_url': 'https://www.bilibili.com/video/BV1'}
        response = self._app(_storage(asset=asset)).get(f'/api/media/{ARTICLE_PK}/source', follow_redirects=False)

        self.assertEqual(404, response.status_code)
        self.assertNotIn('location', response.headers)

    def test_a_foreign_article_is_refused_before_the_store_is_read(self) -> None:
        storage = _storage(asset={'state': 'stored', 's3_key': STORE_KEY, 'size_bytes': 10})
        response = self._app(storage, visible=False).get(f'/api/media/{ARTICLE_PK}/video')

        self.assertEqual(404, response.status_code)
        storage.media_assets.get.assert_not_awaited()


class ParseRangeTest(unittest.TestCase):
    """The Range grammar, independently of the route that calls it."""

    def test_a_closed_range_is_clamped_to_the_file(self) -> None:
        self.assertEqual((10, 19), parse_range('bytes=10-19', 100))
        self.assertEqual((0, 99), parse_range('bytes=0-999', 100))

    def test_an_open_range_runs_to_the_end(self) -> None:
        self.assertEqual((10, None), parse_range('bytes=10-', 100))
        self.assertEqual((10, None), parse_range('bytes=10-', None))

    def test_a_suffix_range_counts_back_from_the_end(self) -> None:
        self.assertEqual((90, 99), parse_range('bytes=-10', 100))
        # Longer than the file: the whole file, not a negative start.
        self.assertEqual((0, 99), parse_range('bytes=-500', 100))
        self.assertEqual((0, 0), parse_range('bytes=-1', 1))

    def test_a_suffix_range_without_a_known_total_is_served_whole(self) -> None:
        # The length cannot be computed, and a wrong one would corrupt playback.
        self.assertEqual((None, None), parse_range('bytes=-10', None))

    def test_an_empty_suffix_has_no_range(self) -> None:
        for total in (100, 0):
            with self.assertRaises(ArchiveError):
                parse_range('bytes=-0', total)

    def test_a_suffix_of_an_empty_object_is_unsatisfiable(self) -> None:
        with self.assertRaises(ArchiveError):
            parse_range('bytes=-10', 0)

    def test_a_range_that_starts_past_the_end_is_unsatisfiable(self) -> None:
        for header in ('bytes=100-', 'bytes=100-200', 'bytes=500-600'):
            with self.assertRaises(ArchiveError):
                parse_range(header, 100)

    def test_a_reversed_range_is_refused(self) -> None:
        with self.assertRaises(ArchiveError):
            parse_range('bytes=50-10', 100)

    def test_spacing_and_case_are_tolerated(self) -> None:
        self.assertEqual((10, 19), parse_range(' BYTES = 10 - 19 ', 100))
        self.assertEqual((90, 99), parse_range('Bytes=-10', 100))

    def test_a_header_we_cannot_read_is_left_to_the_caller_default(self) -> None:
        # Unreadable, not unsatisfiable: the whole file is the safe answer, and
        # a multi-range request is not half-answered.
        for header in (None, '', 'items=0-9', 'bytes=abc', 'bytes=0-9, 20-29', 'bytes=--5'):
            self.assertEqual((None, None), parse_range(header, 100), header)

    def test_a_known_total_is_not_required_for_a_closed_range(self) -> None:
        self.assertEqual((10, 19), parse_range('bytes=10-19', None))


class RangeHeaderShapeTest(unittest.TestCase):
    """What the JSON error body and the ``Content-Range`` header look like."""

    def test_the_416_body_matches_the_api_error_shape(self) -> None:
        asset = {'state': 'stored', 's3_key': STORE_KEY, 'size_bytes': 100}
        storage = _storage(asset=asset)
        with patch.object(article_api, '_assert_article_visible', AsyncMock()):
            response = _client(storage).get(f'/api/media/{ARTICLE_PK}/video', headers={'range': 'bytes=-0'})

        self.assertEqual(416, response.status_code)
        body = response.json()
        self.assertEqual({'error', 'code'}, set(body))
        self.assertEqual('bytes */100', response.headers['content-range'])

    def test_an_unsatisfiable_range_without_a_known_size_omits_the_header(self) -> None:
        asset = {'state': 'stored', 's3_key': STORE_KEY, 'size_bytes': None}
        storage = _storage(asset=asset)
        with patch.object(article_api, '_assert_article_visible', AsyncMock()):
            response = _client(storage).get(f'/api/media/{ARTICLE_PK}/video', headers={'range': 'bytes=5-1'})

        self.assertEqual(416, response.status_code)
        self.assertNotIn('content-range', response.headers)


class ImageVisibilityTest(unittest.IsolatedAsyncioTestCase):
    """Image access follows the subscription rule the article routes already use."""

    async def _call(self, *, row: dict | None):
        captured: dict = {}

        async def fake_fetchone(storage, sql, params=None, normalize=None):
            captured['sql'] = sql
            captured['params'] = params
            return row

        with patch.object(article_queries, 'fetchone_row', fake_fetchone):
            await _ensure_image_visible(SimpleNamespace(), 7, 9)
        return captured

    async def test_the_visibility_query_requires_an_enabled_subscription(self) -> None:
        captured = await self._call(row={'ok': 1})

        self.assertIn('NOT sub.is_disabled', captured['sql'])
        self.assertIn('JOIN subscription sub', captured['sql'])
        # Join parameter first, image id second — the order the SQL binds them in.
        self.assertEqual([7, 9], captured['params'])

    async def test_a_disabled_subscription_is_a_404(self) -> None:
        with self.assertRaises(ApiError) as ctx:
            await self._call(row=None)

        self.assertEqual(404, ctx.exception.status)

    async def test_an_enabled_subscription_passes(self) -> None:
        await self._call(row={'ok': 1})

    async def test_a_foreign_image_is_refused_before_its_bytes_are_fetched(self) -> None:
        fetch_bytes = AsyncMock()
        hidden = AsyncMock(side_effect=ApiError('Image not found', status=404))
        with (
            patch.object(article_queries, '_ensure_image_visible', hidden),
            patch.object(article_queries, 'fetch_image_bytes', fetch_bytes),
            self.assertRaises(ApiError) as ctx,
        ):
            await _fetch_image(SimpleNamespace(), 7, 9)

        self.assertEqual(404, ctx.exception.status)
        fetch_bytes.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
