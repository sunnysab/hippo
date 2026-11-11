"""Caixin refetch uses authenticated full bodies and preserves stored identity."""

import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, Mock, patch

from hippo.api.routers import article as article_api
from hippo.sources import CredentialRejected

ARTICLE_URL = 'https://international.caixin.com/2026-11-11/102500001.html'
PERSONAL_COOKIE = 'UID=7; SA_USER_auth=personal-secret-marker'
INSTANCE_COOKIE = 'UID=9; SA_USER_auth=instance-secret-marker'
PROXY = 'http://proxy.example:8082'
FULL_HTML = '<h2>Complete article</h2><p>Authenticated full body.</p>'


class _AsyncContext:
    def __init__(self, value):
        self._value = value

    async def __aenter__(self):
        return self._value

    async def __aexit__(self, *exc):
        return False


class CaixinRefetchTest(unittest.TestCase):
    def _exercise(self, *, personal_cookie, failure=None):
        row = {
            'biz': 'caixin:132',
            'article_id': '102500001',
            'link': ARTICLE_URL,
            'title': 'Stored title',
            'author': 'Stored reporter',
            'digest': 'Stored list summary',
            'cover': 'https://img.caixin.com/stored-cover.jpg',
            'publish_at': 1794369600,
            'raw_json': json.dumps({'channel_id': 132, 'is_paid': True, 'list_evidence': 'keep'}),
            'account_source': 'caixin',
        }
        source = SimpleNamespace(
            fetch_article=AsyncMock(
                return_value={'title': 'Updated title', 'author': '', 'content_html': FULL_HTML},
                side_effect=failure,
            ),
            aclose=AsyncMock(),
        )
        constructor = Mock(return_value=source)
        downloader = SimpleNamespace(ingest_body=AsyncMock(), download_from_url=AsyncMock())
        thread_storage = SimpleNamespace(
            conn=SimpleNamespace(
                execute=AsyncMock(return_value=SimpleNamespace(fetchone=AsyncMock(return_value=(True,))))
            ),
            credentials=SimpleNamespace(get=AsyncMock(return_value=personal_cookie)),
            transaction=lambda: _AsyncContext(None),
            documents=SimpleNamespace(save=AsyncMock()),
        )
        user = SimpleNamespace(id=7, username='reader', role='user')
        deferred = []

        class _DeferredThread:
            def __init__(self, target=None, daemon=None, **kwargs):
                self._target = target

            def start(self):
                deferred.append(self._target)

        with (
            patch.object(article_api, '_assert_article_visible', AsyncMock()),
            patch.object(article_api, 'fetchone_row', AsyncMock(return_value=row)),
            patch.object(article_api, 'open_storage', lambda: _AsyncContext(thread_storage)),
            patch.object(article_api, 'get_media_settings', AsyncMock(return_value={'caixin_cookie': INSTANCE_COOKIE})),
            patch.object(article_api, 'resolve_proxy', Mock(return_value=PROXY)),
            patch.object(
                article_api,
                'build_downloader_container',
                lambda **kwargs: _AsyncContext(SimpleNamespace(downloader=downloader)),
            ),
            patch.object(article_api, 'CaixinSource', constructor),
            patch.object(article_api.threading, 'Thread', _DeferredThread),
        ):
            started = asyncio.run(article_api.refetch_article(42, storage=SimpleNamespace(), user=user))
            self.addCleanup(article_api._refetch_tasks.pop, started['task_id'], None)
            self.assertEqual(1, len(deferred))
            deferred[0]()
            status = article_api.get_refetch_status(started['task_id'], user=user)
        return SimpleNamespace(
            source=source,
            constructor=constructor,
            downloader=downloader,
            storage=thread_storage,
            status=status,
            row=row,
        )

    def test_refetch_preserves_identity_and_list_metadata_using_the_effective_cookie(self):
        for personal_cookie in (PERSONAL_COOKIE, None):
            with self.subTest(personal_cookie_configured=bool(personal_cookie)):
                run = self._exercise(personal_cookie=personal_cookie)

                run.constructor.assert_called_once_with(
                    cookie=personal_cookie or INSTANCE_COOKIE, proxy=PROXY, pacer=ANY
                )
                run.storage.credentials.get.assert_awaited_once_with(7, 'caixin')
                run.source.fetch_article.assert_awaited_once_with(ARTICLE_URL)
                run.source.aclose.assert_awaited_once()
                run.downloader.download_from_url.assert_not_awaited()
                run.downloader.ingest_body.assert_awaited_once()
                arguments = run.downloader.ingest_body.call_args.kwargs
                self.assertEqual(FULL_HTML, arguments['html'])
                self.assertTrue(arguments['with_images'])
                article = arguments['article']
                self.assertEqual('caixin:132', article.biz)
                self.assertEqual('102500001', article.article_id)
                self.assertEqual(1794369600, article.publish_at)
                self.assertEqual(json.loads(run.row['raw_json']), article.raw)
                self.assertEqual('Updated title', article.title)
                self.assertEqual('Stored reporter', article.author)
                self.assertEqual(run.row['digest'], article.digest)
                self.assertEqual(run.row['cover'], article.cover)
                self.assertEqual(ARTICLE_URL, article.link)
                self.assertEqual('done', run.status['status'])
                self.assertIsNone(run.status['error'])

    def test_rejected_cookie_never_overwrites_the_existing_body_or_leaks_credentials(self):
        run = self._exercise(
            personal_cookie=PERSONAL_COOKIE,
            failure=CredentialRejected('Caixin rejected the cookie or subscription permission'),
        )

        run.source.fetch_article.assert_awaited_once_with(ARTICLE_URL)
        run.source.aclose.assert_awaited_once()
        run.downloader.ingest_body.assert_not_awaited()
        run.downloader.download_from_url.assert_not_awaited()
        self.assertEqual('error', run.status['status'])
        self.assertEqual('error', run.status['phase'])
        self.assertIn('rejected', run.status['error'])
        visible = json.dumps(run.status)
        self.assertNotIn('personal-secret-marker', visible)
        self.assertNotIn('instance-secret-marker', visible)
