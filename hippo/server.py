"""Minimal HTTP server for Hippo API + frontend UI."""

from __future__ import annotations

import contextlib
import logging
import os
import socket
import stat
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from .api.errors import install_exception_handlers
from .api.middleware import install_request_context
from .api.routers import (
    account,
    admin,
    annotation,
    apikey,
    article,
    auth,
    chat,
    daemon,
    feed,
    llm,
    registration,
    report,
    settings,
    source,
)
from .avatar import _ensure_avatar_images_table
from .config import WEB_SERVICE, get_settings
from .logger import configure_logging
from .observability.logging import resolve_level, shutdown_logging
from .observability.otel import init_telemetry, instrument_app
from .report_scheduler import ReportScheduler
from .storage import open_storage
from .sync_scheduler import SyncScheduler

SERVICE_NAME = WEB_SERVICE


def _inprocess_sync_enabled() -> bool:
    return get_settings().web.inprocess_sync


def _normalize_listen_host(host: str | None) -> str | None:
    if host is None:
        return None
    normalized = host.strip()
    return normalized or None


def _normalize_unix_socket_path(unix_socket: Path | str | None) -> Path | None:
    if unix_socket is None:
        return None
    normalized = str(unix_socket).strip()
    if not normalized:
        return None
    return Path(normalized)


def _remove_stale_unix_socket(path: Path) -> None:
    try:
        existing = path.stat()
    except FileNotFoundError:
        return
    if not stat.S_ISSOCK(existing.st_mode):
        raise RuntimeError(f'Unix socket path already exists and is not a socket: {path}')
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        probe.settimeout(0.1)
        probe.connect(str(path))
    except ConnectionRefusedError, FileNotFoundError:
        pass
    except OSError as exc:
        raise RuntimeError(f'Failed to inspect Unix socket path {path}: {exc}') from exc
    else:
        raise RuntimeError(f'Unix socket path is already in use: {path}')
    finally:
        probe.close()
    path.unlink()


def _create_tcp_listen_socket(host: str, port: int) -> socket.socket:
    last_error: OSError | None = None
    addrinfo = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM, flags=socket.AI_PASSIVE)
    for family, socktype, proto, _, sockaddr in addrinfo:
        candidate = socket.socket(family, socktype, proto)
        try:
            candidate.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            candidate.bind(sockaddr)
            candidate.set_inheritable(True)
            return candidate
        except OSError as exc:
            last_error = exc
            candidate.close()
    raise RuntimeError(f'Failed to bind TCP listener on {host}:{port}') from last_error


def _create_unix_listen_socket(path: Path, mode: int) -> socket.socket:
    if not hasattr(socket, 'AF_UNIX'):
        raise RuntimeError('Unix sockets are not supported on this platform')
    if not path.parent.exists():
        raise RuntimeError(f'Unix socket parent directory does not exist: {path.parent}')
    if not path.parent.is_dir():
        raise RuntimeError(f'Unix socket parent path is not a directory: {path.parent}')
    _remove_stale_unix_socket(path)
    candidate = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    bound = False
    try:
        candidate.bind(str(path))
        bound = True
        os.chmod(path, mode)
        candidate.set_inheritable(True)
        return candidate
    except OSError as exc:
        candidate.close()
        if bound:
            with contextlib.suppress(FileNotFoundError):
                path.unlink()
        raise RuntimeError(f'Failed to bind Unix socket on {path}: {exc}') from exc


def _build_listen_sockets(
    *,
    host: str | None,
    port: int | None,
    unix_socket: Path | str | None,
    unix_socket_mode: int = 0o660,
) -> list[socket.socket]:
    normalized_host = _normalize_listen_host(host)
    normalized_unix_socket = _normalize_unix_socket_path(unix_socket)
    sockets: list[socket.socket] = []
    try:
        if normalized_host is not None:
            if port is None:
                raise RuntimeError('TCP port is required when host is configured')
            sockets.append(_create_tcp_listen_socket(normalized_host, port))
        elif port is not None:
            raise RuntimeError('TCP host is required when port is configured')

        if normalized_unix_socket is not None:
            sockets.append(_create_unix_listen_socket(normalized_unix_socket, unix_socket_mode))

        if not sockets:
            raise RuntimeError('At least one listener must be configured')
        return sockets
    except Exception:
        for candidate in sockets:
            candidate.close()
        raise


def create_app(
    static_dir: Path | str = 'frontend/dist',
    *,
    enable_inprocess_sync: bool | None = None,
) -> FastAPI:
    app_settings = get_settings()
    telemetry = init_telemetry(SERVICE_NAME)
    configure_logging(
        level=resolve_level(app_settings.logging.level),
        json_output=app_settings.logging.format != 'console',
        log_file=app_settings.logging.file_for(SERVICE_NAME),
        extra_handlers=telemetry.log_handlers if telemetry else None,
    )
    static_path = Path(static_dir).expanduser().resolve()
    if not static_path.exists():
        raise RuntimeError(f'Static directory not found: {static_path}. Run `npm --prefix frontend build` first.')

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        async with open_storage() as storage:
            # Groups are per-user and created on demand the first time a user
            # opens the app; only the shared avatar cache needs bootstrapping.
            await _ensure_avatar_images_table(storage)
        should_enable_sync = _inprocess_sync_enabled() if enable_inprocess_sync is None else enable_inprocess_sync
        if should_enable_sync:
            app.state.sync_scheduler = SyncScheduler()
            # Both start() methods are coroutines; calling one without awaiting
            # leaves the scheduler created but never running.
            await app.state.sync_scheduler.start()
            app.state.report_scheduler = ReportScheduler()
            await app.state.report_scheduler.start()
        else:
            app.state.sync_scheduler = None
            app.state.report_scheduler = None
        try:
            yield
        finally:
            scheduler = getattr(app.state, 'sync_scheduler', None)
            if scheduler:
                await scheduler.stop()
            report_scheduler = getattr(app.state, 'report_scheduler', None)
            if report_scheduler:
                await report_scheduler.stop()
            if telemetry is not None:
                telemetry.shutdown()
            shutdown_logging()

    app = FastAPI(lifespan=lifespan)
    # Binding the caller's identity is registered before the middleware that
    # can answer without reaching the app, so even those responses are logged
    # with the origin they were decided at.
    install_request_context(app)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=['*'],
        allow_methods=['*'],
        allow_headers=['*'],
    )

    install_exception_handlers(app)
    instrument_app(app)
    for module in (
        auth,
        registration,
        account,
        apikey,
        article,
        source,
        annotation,
        chat,
        report,
        settings,
        feed,
        daemon,
        admin,
        llm,
    ):
        app.include_router(module.router, prefix='/api')
    app.mount('/', StaticFiles(directory=static_path, html=True), name='static')
    return app


def serve(
    host: str | None = None,
    port: int | None = None,
    static_dir: Path | str = 'frontend/dist',
    *,
    unix_socket: Path | str | None = None,
    unix_socket_mode: int = 0o660,
    enable_inprocess_sync: bool | None = None,
) -> None:
    """Serve the API on a Unix socket, or on TCP when a host is given.

    A TCP listener is opt-in: it needs an authenticating front-end in front of
    it, and a listener that only Caddy can reach (the socket) is the safe
    default. Port follows host -- pass one without the other and nothing binds.
    """
    import uvicorn

    app = create_app(static_dir=static_dir, enable_inprocess_sync=enable_inprocess_sync)
    uvicorn_log_level = logging.getLevelName(resolve_level(get_settings().logging.level)).lower()
    listen_sockets = _build_listen_sockets(
        host=host,
        port=port,
        unix_socket=unix_socket,
        unix_socket_mode=unix_socket_mode,
    )
    config = uvicorn.Config(
        app,
        log_level=uvicorn_log_level,
        # Keep the structlog pipeline installed by create_app().
        log_config=None,
    )
    server = uvicorn.Server(config)
    try:
        server.run(sockets=listen_sockets)
    finally:
        for candidate in listen_sockets:
            candidate.close()
