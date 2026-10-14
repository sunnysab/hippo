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
from fastapi.responses import JSONResponse

from ..exceptions import ApiError
from ..logger import get_logger
from .middleware import request_log_fields

logger = get_logger(__name__)


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
        else:
            logger.info('request_failed', **fields)
        return JSONResponse(
            status_code=exc.status,
            content={'error': str(exc), 'code': exc.code},
        )

    @app.exception_handler(Exception)
    async def _handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        logger.exception('request_crashed', **_fields(request))
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={'error': 'Internal server error', 'code': 'internal_error'},
        )
