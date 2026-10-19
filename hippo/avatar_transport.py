"""Avatar-only HTTP(S) proxy tunnel that pins the IP and certifies the name.

httpcore 1.0.9's ``AsyncTunnelHTTPConnection`` has two defects for the avatar
guard: it CONNECTs to an IPv6 address without brackets, and it ignores the
``sni_hostname`` request extension for the origin TLS (so certificate and SNI
would be checked against the pinned IP). Both are corrected here, for the avatar
guard only; direct and SOCKS connections keep httpcore's own path.
"""

from __future__ import annotations

import httpcore
import httpx
from httpcore._async.http11 import AsyncHTTP11Connection
from httpcore._async.http_proxy import AsyncTunnelHTTPConnection, merge_headers
from httpcore._ssl import default_ssl_context


def is_http_proxy(proxy: str) -> bool:
    """True for an HTTP(S) forward proxy, False for a SOCKS proxy."""
    return httpx.URL(proxy).scheme in ('http', 'https')


class _PinnedTunnelHTTPConnection(AsyncTunnelHTTPConnection):
    async def handle_async_request(self, request: httpcore.Request) -> httpcore.Response:
        # Mirrors httpcore 1.0.9 with two fixes: the CONNECT target brackets an
        # IPv6 address, and the origin TLS uses ``sni_hostname`` rather than the
        # pinned IP. The avatar transport is HTTP/1.1 only.
        timeouts = request.extensions.get('timeout', {})
        timeout = timeouts.get('connect', None)
        sni_hostname = request.extensions.get('sni_hostname')
        async with self._connect_lock:
            if not self._connected:
                host = self._remote_origin.host
                port = self._remote_origin.port
                target = b'[%b]:%d' % (host, port) if b':' in host else b'%b:%d' % (host, port)
                connect_url = httpcore.URL(
                    scheme=self._proxy_origin.scheme,
                    host=self._proxy_origin.host,
                    port=self._proxy_origin.port,
                    target=target,
                )
                connect_headers = merge_headers([(b'Host', target), (b'Accept', b'*/*')], self._proxy_headers)
                # Keep the origin SNI off the proxy connection, so an HTTPS proxy
                # is still verified against its own hostname.
                connect_extensions = {key: value for key, value in request.extensions.items() if key != 'sni_hostname'}
                connect_request = httpcore.Request(
                    method=b'CONNECT',
                    url=connect_url,
                    headers=connect_headers,
                    extensions=connect_extensions,
                )
                connect_response = await self._connection.handle_async_request(connect_request)
                if connect_response.status < 200 or connect_response.status > 299:
                    reason = connect_response.extensions.get('reason_phrase', b'').decode('ascii', errors='ignore')
                    await self._connection.aclose()
                    raise httpcore.ProxyError(f'{connect_response.status} {reason}')

                stream = connect_response.extensions['network_stream']
                ssl_context = default_ssl_context() if self._ssl_context is None else self._ssl_context
                ssl_context.set_alpn_protocols(['http/1.1'])
                server_hostname = (
                    sni_hostname.decode('ascii') if isinstance(sni_hostname, bytes) else sni_hostname
                ) or host.decode('ascii')
                stream = await stream.start_tls(
                    ssl_context=ssl_context, server_hostname=server_hostname, timeout=timeout
                )
                self._connection = AsyncHTTP11Connection(
                    origin=self._remote_origin,
                    stream=stream,
                    keepalive_expiry=self._keepalive_expiry,
                )
                self._connected = True
        return await self._connection.handle_async_request(request)


class _PinnedHTTPProxy(httpcore.AsyncHTTPProxy):
    def create_connection(self, origin: httpcore.Origin) -> httpcore.AsyncConnectionInterface:
        if origin.scheme == b'http':
            return super().create_connection(origin)
        return _PinnedTunnelHTTPConnection(
            proxy_origin=self._proxy_url.origin,
            proxy_headers=self._proxy_headers,
            remote_origin=origin,
            ssl_context=self._ssl_context,
            proxy_ssl_context=self._proxy_ssl_context,
            keepalive_expiry=self._keepalive_expiry,
            http1=self._http1,
            http2=False,
            network_backend=self._network_backend,
        )


class PinnedProxyTransport(httpx.AsyncHTTPTransport):
    """httpx transport for avatar fetches through an admin HTTP(S) proxy.

    The guard already pinned the URL to a checked public IP and set the original
    hostname as ``sni_hostname``; this transport is what makes that pair hold on
    the proxy tunnel path. ``handle_async_request`` is inherited from httpx.
    """

    def __init__(
        self,
        proxy: str,
        *,
        limits: httpx.Limits,
        network_backend: httpcore.AsyncNetworkBackend | None = None,
    ) -> None:
        parsed = httpx.Proxy(url=proxy)
        self._pool = _PinnedHTTPProxy(
            proxy_url=httpcore.URL(
                scheme=parsed.url.raw_scheme,
                host=parsed.url.raw_host,
                port=parsed.url.port,
                target=parsed.url.raw_path,
            ),
            proxy_auth=parsed.raw_auth,
            proxy_headers=parsed.headers.raw,
            proxy_ssl_context=parsed.ssl_context,
            ssl_context=httpx.create_ssl_context(),
            max_connections=limits.max_connections,
            max_keepalive_connections=limits.max_keepalive_connections,
            keepalive_expiry=limits.keepalive_expiry,
            network_backend=network_backend,
        )


__all__ = ['PinnedProxyTransport', 'is_http_proxy']
