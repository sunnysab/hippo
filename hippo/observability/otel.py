"""OpenTelemetry instrumentation exported to an OTLP endpoint (SignOz).

Two independent processes initialise this module: ``hippo-web`` (FastAPI) and
``hippo-sync-worker``. They must use different ``service.name`` values, which is
why :func:`init_telemetry` takes the name as an argument instead of reading it
from a module constant.

The endpoint, the TLS mode and the sample ratio come from the ``[observability]``
table in ``config.toml``: the SDK's own ``OTEL_*`` environment variables would
give the same setting two sources of truth. Telemetry stays off while that table
has no endpoint, so the CLI and the test suite stay silent by default.
"""

from __future__ import annotations

import contextlib
import functools
import inspect
import logging
import socket
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.instrumentation.psycopg import PsycopgInstrumentor
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased

from ..config import ObservabilitySettings, get_settings

_TRACER_NAME = 'hippo'


def otlp_endpoint() -> str | None:
    """Return the configured OTLP endpoint, if any."""
    return get_settings().observability.otlp_endpoint or None


def telemetry_enabled() -> bool:
    """Telemetry only turns on when the file enables it and sets an endpoint."""
    settings = get_settings().observability
    return settings.enabled and bool(settings.otlp_endpoint)


def _exporters(
    settings: ObservabilitySettings,
) -> tuple[OTLPSpanExporter, OTLPMetricExporter, OTLPLogExporter]:
    """Build the three exporters from the file, never from the environment.

    The endpoint is passed explicitly: left out, the SDK would fall back to
    ``OTEL_EXPORTER_OTLP_ENDPOINT`` and the two sources could disagree.
    """
    kwargs = {'endpoint': settings.otlp_endpoint, 'insecure': settings.insecure}
    return OTLPSpanExporter(**kwargs), OTLPMetricExporter(**kwargs), OTLPLogExporter(**kwargs)


def _service_version() -> str:
    try:
        from importlib.metadata import version

        return version('hippo')
    except Exception:  # pragma: no cover - metadata is absent when running from a source tree
        return '0.0.0'


@dataclass
class TelemetryRuntime:
    """Handles kept so the process can shut the exporters down cleanly."""

    tracer_provider: TracerProvider
    meter_provider: MeterProvider
    logger_provider: LoggerProvider
    log_handlers: list[logging.Handler] = field(default_factory=list)
    enabled: bool = True

    def shutdown(self) -> None:
        """Flush pending spans, metrics and log records."""
        for provider in (self.tracer_provider, self.meter_provider, self.logger_provider):
            provider.shutdown()


def _install_trace_record_factory() -> None:
    """Stamp trace/span ids onto every LogRecord at creation time.

    The record is built on the calling thread, where the active span is still in
    scope. Rendering happens later on the logging listener thread, so having the
    ids travel on the record is what keeps stdlib log lines correlatable.
    """
    from opentelemetry import trace

    previous = logging.getLogRecordFactory()
    if getattr(previous, '_hippo_trace_factory', False):
        return

    def record_factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
        record = previous(*args, **kwargs)
        context = trace.get_current_span().get_span_context()
        if context.is_valid:
            record.otelTraceID = format(context.trace_id, '032x')  # type: ignore[attr-defined]
            record.otelSpanID = format(context.span_id, '016x')  # type: ignore[attr-defined]
        return record

    record_factory._hippo_trace_factory = True  # type: ignore[attr-defined]
    logging.setLogRecordFactory(record_factory)


def init_telemetry(service_name: str) -> TelemetryRuntime | None:
    """Install trace, metric and log exporters for ``service_name``.

    Returns ``None`` when telemetry is off or no endpoint is configured.
    """
    settings = get_settings().observability
    if not settings.enabled or not settings.otlp_endpoint:
        return None

    resource = Resource.create(
        {
            'service.name': service_name,
            'service.version': _service_version(),
            'deployment.environment': settings.environment,
            'host.name': socket.gethostname(),
        }
    )
    span_exporter, metric_exporter, log_exporter = _exporters(settings)

    sampler = ParentBased(TraceIdRatioBased(settings.sample_ratio))
    tracer_provider = TracerProvider(resource=resource, sampler=sampler)
    tracer_provider.add_span_processor(BatchSpanProcessor(span_exporter))
    trace.set_tracer_provider(tracer_provider)

    metric_reader = PeriodicExportingMetricReader(metric_exporter, export_interval_millis=30_000)
    meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])
    metrics.set_meter_provider(meter_provider)

    logger_provider = LoggerProvider(resource=resource)
    logger_provider.add_log_record_processor(BatchLogRecordProcessor(log_exporter))
    log_handler = LoggingHandler(level=logging.NOTSET, logger_provider=logger_provider)

    # capture_parameters stays off: statement parameters would leak account ids
    # and any credential that happens to travel through SQL.
    PsycopgInstrumentor().instrument(tracer_provider=tracer_provider, capture_parameters=False)
    HTTPXClientInstrumentor().instrument(tracer_provider=tracer_provider)
    # Deliberately no LoggingInstrumentor: it installs a second, unwrapped handler
    # on the root logger (duplicate logs, and it exports structlog's objects as
    # attributes). The trace correlation it would add is done by the factory below.
    _install_trace_record_factory()

    return TelemetryRuntime(
        tracer_provider=tracer_provider,
        meter_provider=meter_provider,
        logger_provider=logger_provider,
        log_handlers=[log_handler],
    )


def instrument_app(app: Any) -> None:
    """Instrument a FastAPI app so requests become server spans."""
    if not telemetry_enabled():
        return
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

    FastAPIInstrumentor.instrument_app(app)


def get_tracer(name: str) -> trace.Tracer:
    return trace.get_tracer(name)


def get_meter(name: str) -> metrics.Meter:
    return metrics.get_meter(name)


@contextlib.contextmanager
def span(name: str, **attributes: Any) -> Iterator[trace.Span]:
    """Run a block inside a span tagged with ``attributes``.

    Exceptions propagate untouched; the SDK marks the span as errored on the way
    out. Attribute values of ``None`` are skipped so callers can pass optional
    context without branching.
    """
    with get_tracer(_TRACER_NAME).start_as_current_span(name) as current:
        for key, value in attributes.items():
            if value is not None:
                current.set_attribute(key, value)
        yield current


def traced(
    name: str,
    **attributes: Any,
) -> Callable[[Callable[..., Awaitable[Any]]], Callable[..., Awaitable[Any]]]:
    """Decorator: run an async function's whole body inside a span."""

    def decorate(func: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
        if not inspect.iscoroutinefunction(func):
            raise TypeError(f'@traced only supports async functions, got {func!r}')
        span_name = name or func.__qualname__

        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            with span(span_name, **attributes):
                return await func(*args, **kwargs)

        return wrapper

    return decorate


def traceparent() -> str:
    """Return the active span's W3C traceparent, or an empty string.

    The daemon SDK calls this before each RPC so its server span joins the
    caller's trace instead of starting a new one.
    """
    context = trace.get_current_span().get_span_context()
    if not context.is_valid:
        return ''
    return f'00-{context.trace_id:032x}-{context.span_id:016x}-{int(context.trace_flags):02x}'


def add_span_attributes(attributes: dict[str, Any]) -> None:
    """Attach attributes to the active span, if there is one."""
    current = trace.get_current_span()
    if current is not None and current.is_recording():
        for key, value in attributes.items():
            current.set_attribute(key, value)


__all__ = [
    'TelemetryRuntime',
    'add_span_attributes',
    'get_meter',
    'get_tracer',
    'init_telemetry',
    'instrument_app',
    'otlp_endpoint',
    'span',
    'telemetry_enabled',
    'traced',
    'traceparent',
]
