"""Request-scoped logging context.

Binding the caller's identity once per request means every log line produced
while handling it carries who it was -- not only the error handler's. The
alternative, threading the request into each logger call, is why the API error
log used to say nothing about its caller.

The context lives in ``structlog`` contextvars, which the pipeline already
merges into every record (see ``hippo.observability.logging``).

One caveat drives the shape here: Starlette puts its own error middleware
outside every user middleware, so a handler for an *unhandled* exception runs
after this middleware has already unbound the context. Handlers therefore read
:func:`request_log_fields` off the request directly; the middleware exists to
enrich everything else logged during the request.
"""

from __future__ import annotations

from typing import Any

import structlog
from fastapi import FastAPI
from starlette.requests import Request

from .deps import client_ip


def request_log_fields(request: Request) -> dict[str, str]:
    """Identity fields worth attaching to every log line for this request."""
    fields = {
        'http_host': request.headers.get('host', ''),
        'user_agent': request.headers.get('user-agent', ''),
    }
    address = client_ip(request)
    if address:
        fields['client_ip'] = address
    # Empty values are noise on every record; absent is the honest signal.
    return {key: value for key, value in fields.items() if value}


class RequestContextMiddleware:
    """Bind client address, host and user agent for one HTTP request."""

    def __init__(self, app: Any) -> None:
        self._app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope['type'] != 'http':
            await self._app(scope, receive, send)
            return
        with structlog.contextvars.bound_contextvars(**request_log_fields(Request(scope))):
            await self._app(scope, receive, send)


def install_request_context(app: FastAPI) -> None:
    """Register the request context middleware on the app."""
    app.add_middleware(RequestContextMiddleware)


__all__ = ['RequestContextMiddleware', 'install_request_context', 'request_log_fields']
