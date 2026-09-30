"""Exception handlers for the API."""

from __future__ import annotations

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

from ..exceptions import ApiError
from ..logger import get_logger

logger = get_logger(__name__)


def install_exception_handlers(app: FastAPI) -> None:
    """Register the shared error handlers on the app."""

    @app.exception_handler(ApiError)
    async def _handle_api_error(request: Request, exc: ApiError) -> JSONResponse:
        logger.warning(
            'API error: path=%s method=%s status=%s message=%s',
            request.url.path,
            request.method,
            exc.status,
            str(exc),
        )
        return JSONResponse(status_code=exc.status, content={'error': str(exc)})

    @app.exception_handler(Exception)
    async def _handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        logger.exception('Unexpected error: %s %s', request.method, request.url.path)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={'error': 'Internal server error'},
        )
