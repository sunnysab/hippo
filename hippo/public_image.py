"""SSRF guard for untrusted public image URLs.

An avatar URL comes from a feed or channel the operator added, not from us, so
fetching it from the server must not reach the box's own network or a cloud
metadata endpoint. A name that answers public on the check and private on the
connect (DNS rebinding) must not slip through either. Every hop is therefore
resolved here, every answer is checked, and the request is pinned to one checked
public IP while the certificate and Host stay bound to the original name.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from typing import Any, NamedTuple

import httpx

#: Avatars are small; a body past this is a mistake or an attack.
MAX_IMAGE_BYTES = 5 * 1024 * 1024
#: Redirect chains an avatar server may take us through before we give up.
MAX_REDIRECTS = 5
#: Wall-clock budget for one fetch, covering DNS, every redirect and the body.
#: httpx timeouts bound connect/read but not the DNS thread, so this bounds the
#: whole guard. Cancelling the lookup only stops the wait: the OS resolver thread
#: is left to finish on its own, a bounded and loop-free residual.
MAX_FETCH_SECONDS = 15.0

_ALLOWED_SCHEMES = ('http', 'https')
#: A malformed or unsupported URL is refused, never raised out of the guard.
_URL_ERRORS = (httpx.InvalidURL, ValueError)
#: Raster types a browser renders safely inline. SVG (and any unknown image/*)
#: is refused: served same-origin it can carry active content.
_ALLOWED_IMAGE_TYPES = frozenset(
    {
        'image/apng',
        'image/avif',
        'image/bmp',
        'image/gif',
        'image/jpeg',
        'image/png',
        'image/tiff',
        'image/vnd.microsoft.icon',
        'image/webp',
        'image/x-icon',
    }
)
#: IPv6 prefixes that tunnel or translate an IPv4 address. We do not fetch
#: avatars over them, and their embedded private address would otherwise pass
#: the global check (an IPv4-mapped address is judged as its embedded IPv4).
_TUNNEL_OR_TRANSLATION = tuple(
    ipaddress.ip_network(cidr)
    for cidr in (
        '64:ff9b::/96',  # NAT64 well-known
        '64:ff9b:1::/48',  # NAT64 local-use
        '2002::/16',  # 6to4
        '2001::/32',  # Teredo
    )
)


class _Plan(NamedTuple):
    """One hop: URL pinned to a checked IP, the Host header, and TLS identity."""

    url: str
    host_header: str
    extensions: dict[str, Any]


def _default_port(scheme: str) -> int:
    return 443 if scheme == 'https' else 80


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def _is_public_ip(value: str) -> bool:
    """A single global unicast address, never internal, loopback or metadata."""
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        return False
    # An IPv4-mapped IPv6 address is judged as the IPv4 address it carries.
    mapped = getattr(ip, 'ipv4_mapped', None)
    if mapped is not None:
        ip = mapped
    if isinstance(ip, ipaddress.IPv6Address) and any(ip in net for net in _TUNNEL_OR_TRANSLATION):
        return False
    if ip.is_multicast or ip.is_reserved or ip.is_link_local or ip.is_unspecified:
        return False
    return ip.is_global


def _host_addresses(host: str, port: int) -> list[str]:
    """Every address DNS answers for ``host``, de-duplicated, in order."""
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    addresses: list[str] = []
    for info in infos:
        address = info[4][0]
        if address not in addresses:
            addresses.append(address)
    return addresses


async def _resolve_public_ip(host: str, port: int) -> str | None:
    """The checked public IP to pin the connection to, or ``None`` to refuse.

    A literal is checked as-is. A name is refused when *any* answer is not
    public: a mixed answer is exactly how rebinding reaches an internal address.
    """
    if _is_ip_literal(host):
        return host if _is_public_ip(host) else None
    addresses = await asyncio.to_thread(_host_addresses, host, port)
    if not addresses:
        return None
    if not all(_is_public_ip(address) for address in addresses):
        return None
    return addresses[0]


def _pin(url: httpx.URL, ip: str) -> str:
    literal = f'[{ip}]' if ':' in ip else ip
    port = url.port or _default_port(url.scheme)
    return f'{url.scheme}://{literal}:{port}{url.raw_path.decode("ascii")}'


def _host_header(host: str, url: httpx.URL) -> str:
    literal = f'[{host}]' if ':' in host else host
    if url.port and url.port != _default_port(url.scheme):
        return f'{literal}:{url.port}'
    return literal


async def _plan(url: str) -> _Plan | None:
    try:
        parsed = httpx.URL(url)
        port = parsed.port or _default_port(parsed.scheme)
    except _URL_ERRORS:
        return None
    if parsed.scheme not in _ALLOWED_SCHEMES or parsed.userinfo or not parsed.host:
        return None
    ip = await _resolve_public_ip(parsed.host, port)
    if ip is None:
        return None
    extensions: dict[str, Any] = {}
    # The TLS certificate is validated against the original name, not the IP.
    if parsed.scheme == 'https' and not _is_ip_literal(parsed.host):
        extensions['sni_hostname'] = parsed.host
    return _Plan(_pin(parsed, ip), _host_header(parsed.host, parsed), extensions)


async def _read_capped(response: httpx.Response, max_bytes: int) -> bytes | None:
    """Read the body in chunks, giving up as soon as it passes the cap."""
    chunks: list[bytes] = []
    total = 0
    async for chunk in response.aiter_bytes():
        total += len(chunk)
        if total > max_bytes:
            return None
        chunks.append(chunk)
    return b''.join(chunks)


def _media_type(response: httpx.Response) -> str:
    return (response.headers.get('content-type') or '').split(';', 1)[0].strip().lower()


async def fetch_public_image(
    client: httpx.AsyncClient,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    max_bytes: int = MAX_IMAGE_BYTES,
    max_redirects: int = MAX_REDIRECTS,
    total_timeout: float = MAX_FETCH_SECONDS,
) -> tuple[bytes, str] | None:
    """Fetch an image from ``url`` without letting it reach the private network.

    Redirects are followed by hand so every hop passes the same check; ``client``
    must not follow them itself. Returns ``(data, content_type)`` or ``None``
    when the URL, any hop, the type or the size is refused, or the whole fetch
    outlasts ``total_timeout``.
    """
    try:
        async with asyncio.timeout(total_timeout):
            return await _fetch_public_image(
                client,
                url,
                headers=headers,
                max_bytes=max_bytes,
                max_redirects=max_redirects,
            )
    except TimeoutError:
        return None


async def _fetch_public_image(
    client: httpx.AsyncClient,
    url: str,
    *,
    headers: dict[str, str] | None,
    max_bytes: int,
    max_redirects: int,
) -> tuple[bytes, str] | None:
    base_headers = dict(headers or {})
    current = url
    for _ in range(max_redirects + 1):
        plan = await _plan(current)
        if plan is None:
            return None
        request_headers = {**base_headers, 'Host': plan.host_header}
        async with client.stream(
            'GET',
            plan.url,
            headers=request_headers,
            extensions=plan.extensions,
            follow_redirects=False,
        ) as response:
            if response.is_redirect:
                location = response.headers.get('location')
                if not location:
                    return None
                try:
                    current = str(httpx.URL(current).join(location))
                except _URL_ERRORS:
                    return None
                continue
            if response.status_code != 200:
                return None
            media_type = _media_type(response)
            if media_type not in _ALLOWED_IMAGE_TYPES:
                return None
            data = await _read_capped(response, max_bytes)
            if data is None:
                return None
            return data, media_type
    return None


__all__ = ['MAX_FETCH_SECONDS', 'MAX_IMAGE_BYTES', 'MAX_REDIRECTS', 'fetch_public_image']
