"""Caixin CLI boundaries and channel ingestion without external services."""

import asyncio
import json
import unittest
from unittest.mock import AsyncMock, Mock, patch

from hippo.sources import CredentialRejected
from hippo.sources.caixin import CaixinError, CaixinSource

ARTICLE_URL = 'https://international.caixin.com/2026-11-11/102500001.html'
SUMMARY = {
    'article_id': 102500001,
    'title': 'An article',
    'url': ARTICLE_URL,
    'publish_time': '2026-11-11 12:00:00',
    'summary': 'Only a summary',
    'image_url': 'https://img.caixin.com/cover.jpg',
    'author': 'Reporter',
    'channel_id': 132,
    'is_paid': True,
}
ARTICLE = {
    'article_id': 102500001,
    'url': ARTICLE_URL,
    'title': 'An article',
    'content_html': '<h2>Full body</h2><p>The whole article.</p>',
}


def process_reply(data=None, *, error=None, returncode=0):
    result = {'ok': True, 'data': data} if error is None else {'ok': False, 'error': error}
    return Mock(
        returncode=returncode,
        communicate=AsyncMock(return_value=(json.dumps(result).encode(), None)),
        wait=AsyncMock(return_value=returncode),
    )


class PreviewTest(unittest.IsolatedAsyncioTestCase):
    async def test_an_article_and_its_channel_resolve_without_network(self):
        source = CaixinSource()
        channel = await source.preview('https://international.caixin.com/')
        article = await source.preview(ARTICLE_URL)

        self.assertEqual(channel, article)
        self.assertEqual('caixin:132', channel.biz)
        self.assertEqual('132', channel.source_key)
        self.assertEqual('caixin', channel.source)

    async def test_unknown_hosts_credentials_ports_and_paths_are_refused(self):
        for url in (
            'https://international.caixin.com.evil.example/',
            'https://international.caixin.com@evil.example/',
            'https://user@international.caixin.com/',
            'https://international.caixin.com:443/',
            'https://international.caixin.com:/',
            'https://international.cai\nxin.com/',
            'https://database.caixin.com/',
            'https://international.caixin.com/search',
            'https://international.caixin.com\\@evil.example/',
            'file:///international.caixin.com/',
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                await CaixinSource().preview(url)


class RequestTest(unittest.IsolatedAsyncioTestCase):
    async def test_cookies_and_proxy_use_stdin_and_never_process_arguments(self):
        process = process_reply(ARTICLE)
        with patch('hippo.sources.caixin.asyncio.create_subprocess_exec', AsyncMock(return_value=process)) as spawn:
            await CaixinSource(
                cookie='UID=7; SA_USER_auth=private-cookie', proxy='http://user:pass@proxy:8082'
            ).fetch_article(ARTICLE_URL)

        self.assertEqual(('caixin', 'fetch', 'article', '--url', ARTICLE_URL), spawn.call_args.args)
        sent = json.loads(process.communicate.call_args.args[0])
        self.assertEqual({'UID': '7', 'SA_USER_auth': 'private-cookie'}, sent['cookies'])
        self.assertEqual('http://user:pass@proxy:8082', sent['proxy'])
        self.assertEqual(asyncio.subprocess.DEVNULL, spawn.call_args.kwargs['stderr'])

    async def test_credential_rejection_allows_the_existing_credential_chain_to_retry(self):
        process = process_reply(error={'code': 'credential_rejected', 'message': 'private-cookie'}, returncode=1)
        with (
            patch('hippo.sources.caixin.asyncio.create_subprocess_exec', AsyncMock(return_value=process)),
            self.assertRaises(CredentialRejected) as caught,
        ):
            await CaixinSource().fetch_article(ARTICLE_URL)
        self.assertNotIn('private-cookie', str(caught.exception))

    async def test_unexpected_cli_errors_never_expose_request_details(self):
        process = process_reply(error={'code': 'unknown', 'message': 'UID=private-cookie'}, returncode=1)
        with (
            patch('hippo.sources.caixin.asyncio.create_subprocess_exec', AsyncMock(return_value=process)),
            self.assertRaises(CaixinError) as caught,
        ):
            await CaixinSource().fetch_article(ARTICLE_URL)
        self.assertNotIn('private-cookie', str(caught.exception))

    async def test_invalid_json_is_a_source_error(self):
        process = process_reply()
        process.communicate.return_value = (b'not json: UID=private-cookie', None)
        with (
            patch('hippo.sources.caixin.asyncio.create_subprocess_exec', AsyncMock(return_value=process)),
            self.assertRaises(CaixinError) as caught,
        ):
            await CaixinSource().fetch_article(ARTICLE_URL)
        self.assertNotIn('private-cookie', str(caught.exception))

    async def test_missing_binary_has_an_actionable_error(self):
        with (
            patch('hippo.sources.caixin.asyncio.create_subprocess_exec', AsyncMock(side_effect=FileNotFoundError)),
            self.assertRaisesRegex(CaixinError, 'install the caixin binary'),
        ):
            await CaixinSource().fetch_article(ARTICLE_URL)

    async def test_timeout_kills_and_reaps_the_child(self):
        process = process_reply()
        process.returncode = None
        process.communicate.side_effect = TimeoutError
        with (
            patch('hippo.sources.caixin.asyncio.create_subprocess_exec', AsyncMock(return_value=process)),
            self.assertRaisesRegex(CaixinError, 'timed out'),
        ):
            await CaixinSource().fetch_article(ARTICLE_URL)
        process.kill.assert_called_once()
        process.wait.assert_awaited_once()

    async def test_cancellation_kills_and_reaps_the_child_then_propagates(self):
        process = process_reply()
        process.returncode = None
        process.communicate.side_effect = asyncio.CancelledError
        with (
            patch('hippo.sources.caixin.asyncio.create_subprocess_exec', AsyncMock(return_value=process)),
            self.assertRaises(asyncio.CancelledError),
        ):
            await CaixinSource().fetch_article(ARTICLE_URL)
        process.kill.assert_called_once()
        process.wait.assert_awaited_once()

    async def test_a_replacement_credential_preserves_proxy_and_binary(self):
        pacer = Mock()
        pacer.request.return_value = unittest.mock.MagicMock()
        pacer.request.return_value.__aenter__ = AsyncMock()
        pacer.request.return_value.__aexit__ = AsyncMock(return_value=False)
        source = CaixinSource(
            cookie='UID=1',
            proxy='http://proxy:8082',
            binary='/opt/bin/caixin',
            proxy_config='/opt/caixin/proxy.toml',
            pacer=pacer,
        )
        process = process_reply(ARTICLE)
        with patch('hippo.sources.caixin.asyncio.create_subprocess_exec', AsyncMock(return_value=process)) as spawn:
            await source.with_credential('UID=2').fetch_article(ARTICLE_URL)
        self.assertEqual('/opt/bin/caixin', spawn.call_args.args[0])
        self.assertEqual(('--proxy-config', '/opt/caixin/proxy.toml'), spawn.call_args.args[1:3])
        pacer.request.assert_called_once_with('article')
        sent = json.loads(process.communicate.call_args.args[0])
        self.assertEqual({'UID': '2'}, sent['cookies'])
        self.assertEqual('http://proxy:8082', sent['proxy'])

    async def test_cookie_header_injection_is_rejected_before_spawning(self):
        with (
            patch('hippo.sources.caixin.asyncio.create_subprocess_exec', AsyncMock()) as spawn,
            self.assertRaises(CaixinError),
        ):
            await CaixinSource(cookie='UID=1\r\nInjected: value').fetch_article(ARTICLE_URL)
        spawn.assert_not_awaited()


class ListingTest(unittest.IsolatedAsyncioTestCase):
    async def test_probe_uses_a_current_paid_article_instead_of_a_fixed_id(self):
        source = CaixinSource()
        with patch.object(source, '_request', AsyncMock(side_effect=[[SUMMARY], ARTICLE])) as request:
            title = await source.probe()
        self.assertEqual('An article', title)
        self.assertEqual(('article', '--url', ARTICLE_URL), request.call_args_list[1].args)

    async def test_a_public_list_alone_does_not_prove_subscription_access(self):
        source = CaixinSource()
        with (
            patch.object(source, '_request', AsyncMock(return_value=[{**SUMMARY, 'is_paid': False}])),
            self.assertRaisesRegex(CaixinError, 'could not be verified'),
        ):
            await source.probe()

    async def test_listing_preserves_metadata_without_requesting_bodies(self):
        source = CaixinSource()
        with patch.object(source, '_request', AsyncMock(side_effect=[[SUMMARY], ARTICLE])) as request:
            listing = await source.list_items(source_key='132', limit=5)

        (item,) = listing.items
        self.assertEqual('102500001', item.article_id)
        self.assertEqual(SUMMARY['summary'], item.digest)
        self.assertIsNone(item.content_html)
        self.assertEqual('', item.media_url)
        self.assertEqual(1794369600, item.publish_at)
        self.assertTrue(listing.is_end)
        self.assertIsNone(listing.next_cursor)
        self.assertEqual(('list', '--channel', '132', '--start', '0', '--count', '5'), request.call_args_list[0].args)
        request.assert_awaited_once()

    async def test_duplicate_ids_are_deduplicated_and_cursor_counts_upstream_rows(self):
        source = CaixinSource()
        with patch.object(source, '_request', AsyncMock(side_effect=[[SUMMARY, SUMMARY], ARTICLE])) as request:
            listing = await source.list_items(source_key='132', limit=2, cursor='4')

        self.assertEqual(1, len(listing.items))
        self.assertEqual(1, request.await_count)
        self.assertFalse(listing.is_end)
        self.assertEqual('6', listing.next_cursor)

    async def test_an_empty_page_ends_pagination(self):
        source = CaixinSource()
        with patch.object(source, '_request', AsyncMock(return_value=[])):
            listing = await source.list_items(source_key='132', limit=5)
        self.assertEqual([], listing.items)
        self.assertTrue(listing.is_end)

    async def test_missing_body_cannot_turn_a_summary_into_a_complete_article(self):
        source = CaixinSource()
        with (
            patch.object(source, '_request', AsyncMock(return_value={**ARTICLE, 'content_html': ''})),
            self.assertRaisesRegex(CaixinError, 'no article body'),
        ):
            await source.fetch_article(ARTICLE_URL)

    async def test_an_upstream_url_cannot_send_credentials_to_another_host(self):
        source = CaixinSource()
        with (
            patch.object(
                source, '_request', AsyncMock(return_value=[{**SUMMARY, 'url': 'https://evil.example/'}])
            ) as request,
            self.assertRaisesRegex(CaixinError, 'unsupported article URL'),
        ):
            await source.list_items(source_key='132', limit=5)
        request.assert_awaited_once()

    async def test_a_body_for_another_article_is_rejected(self):
        source = CaixinSource()
        with (
            patch.object(source, '_request', AsyncMock(return_value={**ARTICLE, 'article_id': 42})),
            self.assertRaisesRegex(CaixinError, 'different article'),
        ):
            await source.fetch_article(ARTICLE_URL)

    async def test_invalid_paging_is_rejected_without_a_process(self):
        source = CaixinSource()
        with patch.object(source, '_request', AsyncMock()) as request:
            for arguments in (
                {'source_key': '999', 'limit': 5},
                {'source_key': '132', 'limit': 0},
                {'source_key': '132', 'limit': 51},
                {'source_key': '132', 'limit': 5, 'cursor': '-1'},
                {'source_key': '132', 'limit': 5, 'cursor': 'not-a-number'},
            ):
                with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                    await source.list_items(**arguments)
        request.assert_not_awaited()
