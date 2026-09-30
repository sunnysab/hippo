"""Structured logging built on structlog with an async-safe sink.

Records are handed to a :class:`logging.handlers.QueueHandler` and rendered on a
dedicated listener thread, so a slow sink (rotating file, OTLP exporter) never
blocks the event loop.

structlog is wired *through* the stdlib ``logging`` package rather than around
it: that keeps uvicorn, psycopg and every other library that logs via
``logging.getLogger`` on the same rendering pipeline, which is also what the
OpenTelemetry logging instrumentation hooks into.
"""

from __future__ import annotations

import atexit
import logging
import logging.handlers
import os
import queue
import sys
from collections.abc import Sequence
from typing import Any

import structlog

#: Keys masked before a record is rendered. Compared case-insensitively.
_REDACT_KEYS = frozenset(
    {
        'api_key',
        'authorization',
        'cookie',
        'password',
        'password_hash',
        'smtp_password',
        'token',
        'token_hash',
    }
)

_LOG_LEVELS = {
    'CRITICAL': logging.CRITICAL,
    'ERROR': logging.ERROR,
    'WARNING': logging.WARNING,
    'INFO': logging.INFO,
    'DEBUG': logging.DEBUG,
}

_LISTENER: logging.handlers.QueueListener | None = None
_CONFIGURED = False


class _AsyncQueueHandler(logging.handlers.QueueHandler):
    """Queue handler that keeps the record payload intact.

    The stock implementation renders the record through its own formatter
    before enqueueing, which destroys the structlog dict that
    :class:`structlog.stdlib.ProcessorFormatter` has to render on the listener
    thread. Handing the record over untouched keeps the two halves compatible.
    """

    def prepare(self, record: logging.LogRecord) -> logging.LogRecord:
        return record


def _redact(_logger: Any, _method_name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    """Mask credential-shaped values so they never reach a sink."""
    for key in event_dict:
        if key.lower() in _REDACT_KEYS:
            event_dict[key] = '***'
    return event_dict


def _add_trace_context(_logger: Any, _method_name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    """Attach the active OpenTelemetry trace/span ids when tracing is enabled.

    Runs on the calling thread, so the contextvars that carry the active span
    are still in scope. Records that arrive through the stdlib logging bridge
    are handled by :func:`_normalize_otel_keys` instead.
    """
    try:
        from opentelemetry import trace
    except ImportError:  # pragma: no cover - tracing is optional
        return event_dict
    context = trace.get_current_span().get_span_context()
    if context.is_valid:
        event_dict['trace_id'] = format(context.trace_id, '032x')
        event_dict['span_id'] = format(context.span_id, '016x')
    return event_dict


def _normalize_otel_keys(_logger: Any, _method_name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    """Promote the correlation ids the record factory stamped on the record.

    ``ProcessorFormatter`` hands structlog records ``event``/``level``/``logger``
    only and stashes the original record under ``_record``; that is where the
    ids added on the calling thread can still be found.
    """
    record = event_dict.get('_record')
    if record is None:
        return event_dict
    trace_id = getattr(record, 'otelTraceID', None)
    span_id = getattr(record, 'otelSpanID', None)
    if trace_id and trace_id != '0' * 32:
        event_dict['trace_id'] = trace_id
    if span_id and span_id != '0' * 16:
        event_dict['span_id'] = span_id
    return event_dict


def _renderer(*, json_output: bool) -> Any:
    if json_output:
        return structlog.processors.JSONRenderer(ensure_ascii=False)
    return structlog.dev.ConsoleRenderer(colors=False)


def _build_handlers(formatter: logging.Formatter, log_file: str | None) -> list[logging.Handler]:
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    handlers: list[logging.Handler] = [console]
    if log_file:
        try:
            log_dir = os.path.dirname(os.path.abspath(log_file))
            if log_dir:
                os.makedirs(log_dir, exist_ok=True)
            file_handler = logging.handlers.TimedRotatingFileHandler(
                log_file, when='midnight', interval=1, backupCount=7, encoding='utf-8'
            )
            file_handler.setFormatter(formatter)
            handlers.append(file_handler)
        except OSError as exc:  # pragma: no cover - depends on the filesystem
            sys.stderr.write(f'Failed to setup file logging: {exc}\n')
    return handlers


def resolve_level(name: str | None) -> int:
    """Map a log-level name to its numeric value, defaulting to WARNING."""
    return _LOG_LEVELS.get(str(name or '').strip().upper(), logging.WARNING)


def configure_logging(
    *,
    level: int = logging.WARNING,
    verbose: bool = False,
    log_file: str | None = None,
    json_output: bool | None = None,
    extra_handlers: Sequence[logging.Handler] | None = None,
) -> None:
    """Install the structlog pipeline on the root logger.

    Args:
        level: Console level when ``verbose`` is off.
        verbose: Log at DEBUG instead of ``level``.
        log_file: Rotating file handler target.
        json_output: Force JSON (``True``) or console (``False``) rendering.
        extra_handlers: Handlers appended to the listener thread, e.g. the
            OpenTelemetry log exporter. They run off the event loop like the
            rest of the sink.

    Safe to call more than once; the previous listener is stopped first.
    """
    global _LISTENER, _CONFIGURED

    if json_output is None:
        json_output = os.environ.get('HIPPO_LOG_FORMAT', 'json').strip().lower() != 'console'

    timestamper = structlog.processors.TimeStamper(fmt='iso', utc=True)
    shared: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.PositionalArgumentsFormatter(),
        timestamper,
        _redact,
        _add_trace_context,
        _normalize_otel_keys,
    ]

    structlog.configure(
        processors=[
            *shared,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            _renderer(json_output=json_output),
        ],
    )

    handlers = _build_handlers(formatter, log_file)
    handlers.extend(extra_handlers or [])
    log_queue: queue.Queue[Any] = queue.Queue(-1)
    queue_handler = _AsyncQueueHandler(log_queue)

    if _LISTENER is not None:
        _LISTENER.stop()
    _LISTENER = logging.handlers.QueueListener(log_queue, *handlers, respect_handler_level=True)
    _LISTENER.start()

    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    root.addHandler(queue_handler)
    root.setLevel(logging.DEBUG if verbose else level)

    # Libraries that install their own handlers would otherwise bypass ours.
    for name in ('uvicorn', 'uvicorn.error', 'uvicorn.access', 'hippo'):
        logger = logging.getLogger(name)
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
        logger.propagate = True

    if not _CONFIGURED:
        atexit.register(shutdown_logging)
        _CONFIGURED = True


def shutdown_logging() -> None:
    """Flush and stop the listener thread."""
    global _LISTENER
    if _LISTENER is not None:
        _LISTENER.stop()
        _LISTENER = None


def get_logger(name: str = 'hippo') -> structlog.stdlib.BoundLogger:
    """Return a bound structlog logger for ``name``."""
    return structlog.get_logger(name)


__all__ = ['configure_logging', 'get_logger', 'resolve_level', 'shutdown_logging']
