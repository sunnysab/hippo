"""Exception handlers for the API.

Each failure is logged as a stable event name plus structured fields, so the
event is low-cardinality and the specifics stay queryable. The fields are read
off the request rather than from the request-context middleware: an *unhandled*
exception is processed by Starlette's outermost error middleware, which runs
after the context middleware has unbound.

The response carries the machine-readable ``code`` next to the human message,
which lets a client branch on the failure without matching prose.
"""

from __future__ import annotations

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from ..exceptions import ApiError
from ..logger import get_logger
from .middleware import request_log_fields

logger = get_logger(__name__)

#: Stable identifier for a request that does not match its declared shape.
VALIDATION_FAILED = 'request.invalid'


def _fields(request: Request) -> dict[str, object]:
    fields: dict[str, object] = dict(request_log_fields(request))
    fields['method'] = request.method
    fields['path'] = request.url.path
    user_id = getattr(request.state, 'user_id', None)
    if user_id is not None:
        fields['user_id'] = user_id
    return fields


def install_exception_handlers(app: FastAPI) -> None:
    """Register the shared error handlers on the app."""

    @app.exception_handler(ApiError)
    async def _handle_api_error(request: Request, exc: ApiError) -> JSONResponse:
        fields = _fields(request)
        fields['code'] = exc.code
        fields['status'] = exc.status
        if exc.status >= 500:
            # Our fault, and the only case worth a traceback: the cause of a 4xx
            # is the caller's own input.
            logger.error('request_failed', exc_info=exc, **fields)
        elif exc.status == 401:
            # A routine session expiry, and the client redirects to the login
            # form on it. Kept below the production level (WARNING) so it does
            # not drown the log; every other 4xx stays at warning, because a 403
            # or a 429 is something an operator may need to see.
            logger.info('request_failed', **fields)
        else:
            logger.warning('request_failed', **fields)
        return JSONResponse(
            status_code=exc.status,
            content={'error': str(exc), 'code': exc.code},
        )

    @app.exception_handler(StarletteHTTPException)
    async def _handle_http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        """Answer the failures Starlette raises itself in the same envelope.

        An unrouted path and a method a route does not allow are answered before
        a route ever runs, so they would otherwise keep ``{'detail': ...}`` and
        break the one-body contract the rest of the API follows.
        """
        fields = _fields(request)
        code = f'http_{exc.status_code}'
        fields['code'] = code
        fields['status'] = exc.status_code
        logger.warning('request_failed', **fields)
        return JSONResponse(
            status_code=exc.status_code,
            content={'error': str(exc.detail), 'code': code},
            headers=getattr(exc, 'headers', None),
        )

    @app.exception_handler(RequestValidationError)
    async def _handle_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        """Answer a malformed request in the same envelope as every other failure.

        FastAPI's own body is ``{'detail': [[...]]}``; a client that only knows
        ``{error, code}`` should not have to parse two shapes for one kind of
        mistake. The details stay in the message, never as a nested structure.
        """
        fields = _fields(request)
        fields['code'] = VALIDATION_FAILED
        logger.warning('request_failed', **fields)
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content={'error': _validation_message(exc), 'code': VALIDATION_FAILED},
        )

    @app.exception_handler(Exception)
    async def _handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        logger.exception('request_crashed', **_fields(request))
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={'error': 'Internal server error', 'code': 'internal_error'},
        )


def _validation_message(exc: RequestValidationError) -> str:
    """One line naming every field that failed, in FastAPI's own words."""
    parts = []
    for error in exc.errors():
        location = '.'.join(str(item) for item in error.get('loc', ()))
        message = str(error.get('msg') or 'invalid value')
        parts.append(f'{location}: {message}' if location else message)
    return '; '.join(parts) or 'Request validation failed'
