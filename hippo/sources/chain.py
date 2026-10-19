"""Which credential to try for one media account.

A media account is a shared catalogue row, but a cookie belongs to one person: the
same Bilibili UP can be followed by several users, each with their own login. So
"whose cookie" has no single answer, and the order is fixed instead -- the
followers' own first, the instance's last. The instance cookie is the one that may
be stale or absent, and a user's own is the one they can see and refresh.

Only a credential refusal moves on to the next candidate. A network error or a
deleted video fails the same way with every cookie, and retrying those would
multiply the requests for nothing.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from ..storage import PostgresStorage
from . import CredentialRejected


async def credentials_for(
    storage: PostgresStorage,
    *,
    biz: str,
    source: str,
    instance: str,
) -> tuple[str, ...]:
    """The secrets to try for ``biz``, in order; the instance one is last.

    An empty ``secret`` means "no credential", which is a legitimate attempt for a
    source that also serves anonymous callers.
    """
    if not biz or not source:
        return (instance,)
    own = await storage.credentials.for_biz(biz, source)
    ordered = [secret for secret in own if secret and secret != instance]
    ordered.append(instance)
    return tuple(ordered)


async def run_with_fallback[T](
    template: Any,
    secrets: tuple[str, ...],
    attempt: Callable[[Any], Awaitable[T]],
) -> T:
    """Run ``attempt`` with each credential until one is accepted.

    Every candidate is built from ``template`` with its own secret, including the
    last one: which secret is the instance's is the caller's business (see
    ``credentials_for``), and guessing here would silently skip a candidate.
    Rebuilding is cheap -- ``with_credential`` shares the connection pool.

    Raises the last refusal when every candidate is refused.
    """
    last: CredentialRejected | None = None
    for secret in secrets:
        try:
            return await attempt(template.with_credential(secret))
        except CredentialRejected as exc:
            last = exc
    if last is None:  # pragma: no cover - secrets is never empty in practice
        raise CredentialRejected('没有可用的凭证')
    raise last


__all__ = ['credentials_for', 'run_with_fallback']
