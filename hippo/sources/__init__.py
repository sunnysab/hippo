"""Source adapters.

A source is whatever a subscription points at: a WeChat account today, a Bilibili
UP, a YouTube channel or a podcast feed. The contract is the one ``weixin_worker``
already implements — list items and turn them into article rows — because that is
the only shape the sync service needs, and keeping the same shape is what makes
adding a source a data change rather than a second sync pipeline.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

from ..weixin_worker import SyncStats


class UnsupportedSource(RuntimeError):
    """No adapter is registered for this ``accounts.source`` value."""


class SourceError(RuntimeError):
    """An adapter failure whose message is written for the person running hippo.

    Subclasses say what the operator should do about it -- supply a cookie, fix
    the URL, check the network -- so the API may show the message as-is. Anything
    else an adapter raises is an implementation detail: it is logged, not served.
    """


class CredentialRejected(SourceError):
    """The source refused the request because the credential is missing or stale.

    Distinct from the other failures on purpose: only this one is worth retrying
    with somebody else's cookie (see ``registry.MediaSources.credential_chain``).
    A network error or a deleted video would fail identically with every
    credential, and retrying those would multiply the requests for nothing.
    """


class SourceAdapter(Protocol):
    async def sync_account(
        self,
        *,
        biz: str,
        source_key: str,
        pages: int = 1,
        offset: str = '',
    ) -> SyncStats: ...


class BodyAdapter(SourceAdapter, Protocol):
    """A source whose bodies arrive through a queue this process drains.

    WeChat works this way: the list phase only enqueues, and ``drain`` downloads
    the bodies. Media sources do not implement this — their bodies come from the
    transcription queue, which the worker runs on its own schedule.
    """

    async def drain(self, *, limit: int | None = None) -> SyncStats: ...


def adapter_for(adapters: Mapping[str, SourceAdapter], source: str) -> SourceAdapter:
    """Look up an adapter, naming the source when there is none.

    A missing adapter is a configuration mistake an operator can act on, so it
    gets a sentence rather than a ``KeyError``.
    """
    adapter = adapters.get(source)
    if adapter is None:
        known = ', '.join(sorted(adapters)) or '无'
        raise UnsupportedSource(f'还没有 {source!r} 来源的适配器（已注册：{known}）')
    return adapter


def body_adapters(adapters: Mapping[str, SourceAdapter]) -> list[BodyAdapter]:
    """The adapters that own a body queue, in a stable order.

    Duck-typed rather than ``isinstance``: a protocol check would need
    ``@runtime_checkable`` and only ever test for the same attribute.
    """
    return [adapter for _, adapter in sorted(adapters.items()) if callable(getattr(adapter, 'drain', None))]


__all__ = [
    'BodyAdapter',
    'CredentialRejected',
    'SourceAdapter',
    'SourceError',
    'UnsupportedSource',
    'adapter_for',
    'body_adapters',
]
