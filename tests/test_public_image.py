"""SSRF guard for untrusted avatar image URLs.

Every test is offline: DNS is faked through ``_host_addresses`` and the transport
is ``httpx.MockTransport``, so no host is ever contacted. The cases mirror the
threat: a private target, a mixed DNS answer, an internal redirect, a rebound
name, a tunnel/translation prefix, an active-content type, a slow drip, and the
legit CDN/feed avatars that must keep working. A fake ``httpcore`` network
backend backs the connection-isolation test.
"""

from __future__ import annotations

import asyncio
import time
import unittest
from unittest.mock import patch

import httpcore
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

    async def _fetch_with_type(
        self, media_type: str, *, body: bytes = b'image-bytes'
    ) -> tuple[tuple[bytes, str] | None, list[httpx.Request]]:
        calls: list[httpx.Request] = []
        client = await self._client(
            lambda request: calls.append(request) or self._image(body=body, content_type=media_type)
        )
        with patch('hippo.public_image._host_addresses', return_value=[PUBLIC_IP]):
            result = await fetch_public_image(client, f'https://{CDN}/avatar')
        return result, calls

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

    async def test_nat64_6to4_and_teredo_literals_are_refused(self) -> None:
        calls: list[httpx.Request] = []
        client = await self._client(lambda request: calls.append(request) or self._image())

        # Each prefix carries or embeds an IPv4 address; none of them is fetched,
        # so an embedded private address can never be reached through a tunnel.
        for url in (
            'http://[64:ff9b::8.8.8.8]/x',  # NAT64 well-known
            'http://[64:ff9b:1::a00:1]/x',  # NAT64 local-use
            'http://[2002:a00:1::]/x',  # 6to4 embedding 10.0.0.1
            'http://[2001::1]/x',  # Teredo
        ):
            with self.subTest(url=url):
                self.assertIsNone(await fetch_public_image(client, url))

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

    async def test_svg_and_unknown_image_types_are_refused(self) -> None:
        # SVG carries active content when served same-origin; unknown image/*
        # types are refused too instead of being trusted by prefix.
        for media_type in ('image/svg+xml', 'image/x-unknown', 'image/x-shockwave-flash'):
            with self.subTest(media_type=media_type):
                result, calls = await self._fetch_with_type(media_type)
                self.assertIsNone(result)
                self.assertEqual(1, len(calls))

    async def test_missing_content_type_is_refused(self) -> None:
        calls: list[httpx.Request] = []
        client = await self._client(lambda request: calls.append(request) or httpx.Response(200, content=b'no-type'))

        with patch('hippo.public_image._host_addresses', return_value=[PUBLIC_IP]):
            self.assertIsNone(await fetch_public_image(client, f'https://{CDN}/avatar'))

        self.assertEqual(1, len(calls))

    async def test_common_raster_image_types_are_returned(self) -> None:
        for media_type in ('image/jpeg', 'image/png', 'image/gif', 'image/webp', 'image/avif', 'image/bmp'):
            with self.subTest(media_type=media_type):
                result, _ = await self._fetch_with_type(media_type, body=b'raster')
                self.assertEqual((b'raster', media_type), result)

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

    async def test_total_timeout_stops_a_slow_response(self) -> None:
        class _SlowBody(httpx.AsyncByteStream):
            async def __aiter__(self):
                while True:
                    await asyncio.sleep(0.5)
                    yield b'x'

            async def aclose(self) -> None:
                pass

        client = await self._client(
            lambda request: httpx.Response(200, headers={'Content-Type': 'image/png'}, stream=_SlowBody())
        )

        with patch('hippo.public_image._host_addresses', return_value=[PUBLIC_IP]):
            started = time.monotonic()
            result = await fetch_public_image(client, f'https://{CDN}/drip.png', total_timeout=0.1)
            elapsed = time.monotonic() - started

        self.assertIsNone(result)
        self.assertLess(elapsed, 2.0)

    async def test_total_timeout_covers_the_dns_lookup(self) -> None:
        # The lookup runs in a thread, so httpx timeouts do not bound it; the
        # guard's own deadline does. The thread finishes on its own afterwards.
        def slow_lookup(host: str, port: int) -> list[str]:
            time.sleep(0.3)
            return [PUBLIC_IP]

        client = await self._client(lambda request: self._image())
        with patch('hippo.public_image._host_addresses', side_effect=slow_lookup):
            started = time.monotonic()
            result = await fetch_public_image(client, f'https://{CDN}/x.png', total_timeout=0.05)
            elapsed = time.monotonic() - started

        self.assertIsNone(result)
        self.assertLess(elapsed, 0.25)

    async def test_dns_lookup_runs_off_the_event_loop(self) -> None:
        ticks: list[int] = []

        def slow_lookup(host: str, port: int) -> list[str]:
            time.sleep(0.1)
            return [PUBLIC_IP]

        client = await self._client(lambda request: self._image())

        async def ticker() -> None:
            for _ in range(5):
                ticks.append(1)
                await asyncio.sleep(0.01)

        with patch('hippo.public_image._host_addresses', side_effect=slow_lookup):
            task = asyncio.create_task(ticker())
            await fetch_public_image(client, f'https://{CDN}/x.png')
            await task

        self.assertTrue(ticks)


class _FakeNetworkStream(httpcore.AsyncNetworkStream):
    """A socket that answers every request with a complete keep-alive image."""

    def __init__(self, backend: _FakeNetworkBackend) -> None:
        self._backend = backend
        self._to_read = bytearray()

    async def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        data = bytes(self._to_read[:max_bytes])
        del self._to_read[:max_bytes]
        return data

    async def write(self, buffer: bytes, timeout: float | None = None) -> None:
        self._to_read += b'HTTP/1.1 200 OK\r\nContent-Type: image/png\r\nContent-Length: 3\r\n\r\nabc'

    async def aclose(self) -> None:
        self._backend.closes += 1

    async def start_tls(
        self,
        ssl_context: object,
        server_hostname: str | None = None,
        timeout: float | None = None,
    ) -> _FakeNetworkStream:
        self._backend.handshakes.append(server_hostname)
        return self

    def get_extra_info(self, info: str) -> object:
        return None


class _FakeNetworkBackend(httpcore.AsyncNetworkBackend):
    """Records every TCP connect and TLS handshake, with no real socket."""

    def __init__(self) -> None:
        self.connects: list[tuple[str, int]] = []
        self.handshakes: list[str | None] = []
        self.closes = 0

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: object = None,
    ) -> _FakeNetworkStream:
        self.connects.append((host, port))
        return _FakeNetworkStream(self)

    async def connect_unix_socket(self, path: str, timeout: float | None = None, socket_options: object = None):
        raise NotImplementedError

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


class ConnectionIsolationTest(unittest.IsolatedAsyncioTestCase):
    """The pool keys by pinned IP, so keep-alive must not span hostnames.

    Two hostnames resolving to the same checked IP share one pool origin. If a
    kept-alive channel were reused, the second hostname would ride a TLS channel
    whose certificate was checked for the first name only. ``max_keepalive_
    connections=0`` (what the avatar client sets) forces a handshake per hop.
    """

    async def _handshakes_for_two_hostnames(self, *, max_keepalive: int | None) -> _FakeNetworkBackend:
        backend = _FakeNetworkBackend()
        pool = httpcore.AsyncConnectionPool(
            network_backend=backend,
            max_keepalive_connections=max_keepalive,
        )
        for hostname in ('a.example', 'b.example'):
            request = httpcore.Request(
                'GET',
                'https://93.184.216.34/a.png',
                headers=[(b'host', b'a.example')],
                extensions={'sni_hostname': hostname},
            )
            response = await pool.handle_async_request(request)
            await response.aread()
            await response.aclose()
        await pool.aclose()
        return backend

    async def test_without_keepalive_each_hostname_gets_its_own_handshake(self) -> None:
        backend = await self._handshakes_for_two_hostnames(max_keepalive=0)

        self.assertEqual([('93.184.216.34', 443)] * 2, backend.connects)
        self.assertEqual(['a.example', 'b.example'], backend.handshakes)

    async def test_keepalive_would_reuse_the_channel_and_skip_the_second_handshake(self) -> None:
        backend = await self._handshakes_for_two_hostnames(max_keepalive=None)

        self.assertEqual([('93.184.216.34', 443)], backend.connects)
        self.assertEqual(['a.example'], backend.handshakes)


if __name__ == '__main__':
    unittest.main()
