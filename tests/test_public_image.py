"""SSRF guard for untrusted avatar image URLs.

Every test is offline: DNS is faked through ``_host_addresses`` and the transport
is ``httpx.MockTransport``, so no host is ever contacted. The cases mirror the
threat: a private target, a mixed DNS answer, an internal redirect, a rebound
name, and the legit CDN/feed avatars that must keep working.
"""

import unittest
from unittest.mock import patch

import httpx

from hippo.public_image import MAX_REDIRECTS, fetch_public_image

CDN = 'cdn.example.com'
PUBLIC_IP = '93.184.216.34'
FEED_IP = '1.1.1.1'


def _resolver(mapping: dict[str, list[str]]):
    """Fake DNS: unknown names resolve private, so a miss can never reach out."""

    def resolve(host: str, port: int) -> list[str]:
        return mapping.get(host, ['127.0.0.1'])

    return resolve


class _Chunks(httpx.AsyncByteStream):
    """A response body that is produced lazily, one chunk at a time."""

    def __init__(self, chunks: list[bytes], yielded: list[bytes]) -> None:
        self._chunks = chunks
        self._yielded = yielded

    async def __aiter__(self):
        for chunk in self._chunks:
            self._yielded.append(chunk)
            yield chunk

    async def aclose(self) -> None:
        pass


class PublicImageTest(unittest.IsolatedAsyncioTestCase):
    async def _client(self, handler) -> httpx.AsyncClient:
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True)
        self.addAsyncCleanup(client.aclose)
        return client

    @staticmethod
    def _image(*, body: bytes = b'image-bytes', content_type: str = 'image/png') -> httpx.Response:
        return httpx.Response(200, headers={'Content-Type': content_type}, content=body)

    async def test_private_ip_literal_is_refused_before_any_request(self) -> None:
        calls: list[httpx.Request] = []
        client = await self._client(lambda request: calls.append(request) or self._image())

        for url in ('http://169.254.169.254/latest/meta-data/', 'http://10.0.0.5/x', 'http://[::1]/x'):
            with self.subTest(url=url):
                self.assertIsNone(await fetch_public_image(client, url))

        self.assertEqual([], calls)

    async def test_ipv4_mapped_private_literal_is_refused(self) -> None:
        calls: list[httpx.Request] = []
        client = await self._client(lambda request: calls.append(request) or self._image())

        self.assertIsNone(await fetch_public_image(client, 'http://[::ffff:192.168.0.1]/x'))
        self.assertEqual([], calls)

    async def test_localhost_name_is_refused(self) -> None:
        calls: list[httpx.Request] = []
        client = await self._client(lambda request: calls.append(request) or self._image())

        with patch('hippo.public_image._host_addresses', return_value=['127.0.0.1']):
            self.assertIsNone(await fetch_public_image(client, 'http://localhost/x'))

        self.assertEqual([], calls)

    async def test_mixed_public_and_private_answers_are_refused(self) -> None:
        calls: list[httpx.Request] = []
        client = await self._client(lambda request: calls.append(request) or self._image())

        for answers in ([PUBLIC_IP, '10.0.0.1'], ['10.0.0.1', PUBLIC_IP]):
            with self.subTest(answers=answers), patch('hippo.public_image._host_addresses', return_value=answers):
                self.assertIsNone(await fetch_public_image(client, f'https://{CDN}/x.png'))

        self.assertEqual([], calls)

    async def test_unresolvable_host_is_refused(self) -> None:
        calls: list[httpx.Request] = []
        client = await self._client(lambda request: calls.append(request) or self._image())

        with patch('hippo.public_image._host_addresses', return_value=[]):
            self.assertIsNone(await fetch_public_image(client, f'https://{CDN}/x.png'))

        self.assertEqual([], calls)

    async def test_resolution_happens_once_and_the_connection_is_pinned(self) -> None:
        calls: list[httpx.Request] = []
        client = await self._client(lambda request: calls.append(request) or self._image())
        # A rebinding name: the second lookup would answer private, but there is
        # no second lookup, and the request goes to the checked IP, not the name.
        answers = iter([[PUBLIC_IP], ['10.0.0.1'], ['10.0.0.1']])

        with patch('hippo.public_image._host_addresses', side_effect=lambda host, port: next(answers)):
            result = await fetch_public_image(client, f'https://{CDN}/x.png')

        self.assertEqual((b'image-bytes', 'image/png'), result)
        self.assertEqual(1, len(calls))
        self.assertEqual(PUBLIC_IP, calls[0].url.host)

    async def test_userinfo_is_refused(self) -> None:
        calls: list[httpx.Request] = []
        client = await self._client(lambda request: calls.append(request) or self._image())

        self.assertIsNone(await fetch_public_image(client, f'https://user:pass@{CDN}/x.png'))
        self.assertEqual([], calls)

    async def test_non_http_scheme_is_refused(self) -> None:
        calls: list[httpx.Request] = []
        client = await self._client(lambda request: calls.append(request) or self._image())

        for url in ('ftp://cdn.example.com/x', 'file:///etc/passwd'):
            with self.subTest(url=url):
                self.assertIsNone(await fetch_public_image(client, url))

        self.assertEqual([], calls)

    async def test_https_request_is_pinned_to_the_checked_ip_with_original_host_and_sni(self) -> None:
        calls: list[httpx.Request] = []
        client = await self._client(lambda request: calls.append(request) or self._image(body=b'cdn'))

        with patch('hippo.public_image._host_addresses', return_value=[PUBLIC_IP]):
            result = await fetch_public_image(
                client, f'https://{CDN}/avatar.png', headers={'User-Agent': 'Mozilla/5.0'}
            )

        self.assertEqual((b'cdn', 'image/png'), result)
        request = calls[0]
        self.assertEqual(PUBLIC_IP, request.url.host)
        self.assertEqual(CDN, request.headers['host'])
        self.assertEqual(CDN, request.extensions['sni_hostname'])
        self.assertEqual('Mozilla/5.0', request.headers['user-agent'])

    async def test_http_request_is_pinned_without_sni(self) -> None:
        calls: list[httpx.Request] = []
        client = await self._client(lambda request: calls.append(request) or self._image(content_type='image/jpeg'))

        with patch('hippo.public_image._host_addresses', return_value=[PUBLIC_IP]):
            result = await fetch_public_image(client, f'http://{CDN}/avatar.jpg')

        self.assertEqual((b'image-bytes', 'image/jpeg'), result)
        request = calls[0]
        self.assertEqual(PUBLIC_IP, request.url.host)
        self.assertEqual(CDN, request.headers['host'])
        self.assertNotIn('sni_hostname', request.extensions)

    async def test_non_default_port_is_kept(self) -> None:
        calls: list[httpx.Request] = []
        client = await self._client(lambda request: calls.append(request) or self._image())

        with patch('hippo.public_image._host_addresses', return_value=[PUBLIC_IP]):
            self.assertIsNotNone(await fetch_public_image(client, f'https://{CDN}:8443/avatar.png'))

        request = calls[0]
        self.assertEqual(PUBLIC_IP, request.url.host)
        self.assertEqual(8443, request.url.port)
        self.assertEqual(f'{CDN}:8443', request.headers['host'])

    async def test_redirect_to_an_internal_host_is_refused(self) -> None:
        calls: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return httpx.Response(302, headers={'Location': 'http://internal.example.com/avatar.png'})

        client = await self._client(handler)
        resolver = _resolver({CDN: [PUBLIC_IP], 'internal.example.com': ['10.0.0.5']})

        with patch('hippo.public_image._host_addresses', side_effect=resolver):
            self.assertIsNone(await fetch_public_image(client, f'https://{CDN}/avatar.png'))

        self.assertEqual(1, len(calls))

    async def test_redirect_chain_is_validated_hop_by_hop(self) -> None:
        calls: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            if request.url.path == '/a':
                return httpx.Response(302, headers={'Location': '/b'})
            if request.url.path == '/b':
                return httpx.Response(302, headers={'Location': 'https://other.example.com/c'})
            return self._image(body=b'final', content_type='image/webp')

        client = await self._client(handler)
        resolver = _resolver({CDN: [PUBLIC_IP], 'other.example.com': [FEED_IP]})

        with patch('hippo.public_image._host_addresses', side_effect=resolver):
            result = await fetch_public_image(client, f'https://{CDN}/a')

        self.assertEqual((b'final', 'image/webp'), result)
        self.assertEqual([PUBLIC_IP, PUBLIC_IP, FEED_IP], [request.url.host for request in calls])
        self.assertEqual([CDN, CDN, 'other.example.com'], [request.headers['host'] for request in calls])

    async def test_redirect_loop_is_limited(self) -> None:
        calls: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return httpx.Response(302, headers={'Location': '/next'})

        client = await self._client(handler)

        with patch('hippo.public_image._host_addresses', return_value=[PUBLIC_IP]):
            self.assertIsNone(await fetch_public_image(client, f'https://{CDN}/start'))

        self.assertEqual(MAX_REDIRECTS + 1, len(calls))

    async def test_redirect_without_location_is_refused(self) -> None:
        calls: list[httpx.Request] = []
        client = await self._client(lambda request: calls.append(request) or httpx.Response(302))

        with patch('hippo.public_image._host_addresses', return_value=[PUBLIC_IP]):
            self.assertIsNone(await fetch_public_image(client, f'https://{CDN}/x'))

        self.assertEqual(1, len(calls))

    async def test_non_image_content_type_is_refused(self) -> None:
        calls: list[httpx.Request] = []
        client = await self._client(
            lambda request: (
                calls.append(request)
                or httpx.Response(200, headers={'Content-Type': 'text/html'}, content=b'<html>login</html>')
            )
        )

        with patch('hippo.public_image._host_addresses', return_value=[PUBLIC_IP]):
            self.assertIsNone(await fetch_public_image(client, f'https://{CDN}/avatar.png'))

        self.assertEqual(1, len(calls))

    async def test_oversized_body_is_stopped_mid_stream(self) -> None:
        yielded: list[bytes] = []
        client = await self._client(
            lambda request: httpx.Response(
                200,
                headers={'Content-Type': 'image/png'},
                stream=_Chunks([b'x' * 1024] * 10, yielded),
            )
        )

        with patch('hippo.public_image._host_addresses', return_value=[PUBLIC_IP]):
            result = await fetch_public_image(client, f'https://{CDN}/huge.png', max_bytes=2048)

        self.assertIsNone(result)
        # The cap tripped while streaming: the rest of the body was never pulled.
        self.assertGreaterEqual(len(yielded), 1)
        self.assertLess(len(yielded), 10)

    async def test_body_at_the_cap_is_returned(self) -> None:
        body = b'y' * 2048
        client = await self._client(lambda request: self._image(body=body))

        with patch('hippo.public_image._host_addresses', return_value=[PUBLIC_IP]):
            result = await fetch_public_image(client, f'https://{CDN}/ok.png', max_bytes=2048)

        self.assertEqual((body, 'image/png'), result)

    async def test_legit_public_cdn_avatar_is_returned(self) -> None:
        calls: list[httpx.Request] = []
        client = await self._client(lambda request: calls.append(request) or self._image(body=b'yt-avatar'))

        with patch('hippo.public_image._host_addresses', return_value=[PUBLIC_IP]):
            result = await fetch_public_image(client, 'https://yt3.googleusercontent.com/ytc/abc=s88.jpg')

        self.assertEqual((b'yt-avatar', 'image/png'), result)
        self.assertEqual(1, len(calls))

    async def test_arbitrary_public_feed_avatar_is_returned(self) -> None:
        calls: list[httpx.Request] = []
        client = await self._client(lambda request: calls.append(request) or self._image(content_type='image/jpeg'))

        with patch('hippo.public_image._host_addresses', return_value=[FEED_IP]):
            result = await fetch_public_image(client, 'https://media.rss-cdn.example/avatar.jpg')

        self.assertEqual((b'image-bytes', 'image/jpeg'), result)
        self.assertEqual(FEED_IP, calls[0].url.host)


if __name__ == '__main__':
    unittest.main()
