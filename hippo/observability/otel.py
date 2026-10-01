"""OpenTelemetry instrumentation exported to an OTLP endpoint (SignOz).

Two independent processes initialise this module: ``hippo-web`` (FastAPI) and
``hippo-sync-worker``. They must use different ``service.name`` values, which is
why :func:`init_telemetry` takes the name as an argument instead of reading it
from a module constant.

Tracing is off unless an OTLP endpoint is configured, so the CLI and the test
suite stay silent by default.
"""

from __future__ import annotations

import logging
import os
import socket
from dataclasses import dataclass, field
from typing import Any

from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.instrumentation.logging import LoggingInstrumentor
from opentelemetry.instrumentation.psycopg import PsycopgInstrumentor
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased

_DISABLED_VALUES = {'0', 'false', 'no', 'off'}
_DEFAULT_SAMPLE_RATIO = 0.1


def _env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in _DISABLED_VALUES


def otlp_endpoint() -> str | None:
    """Return the configured OTLP endpoint, if any."""
    return os.environ.get('OTEL_EXPORTER_OTLP_ENDPOINT') or os.environ.get('OTEL_EXPORTER_OTLP_TRACES_ENDPOINT') or None


def telemetry_enabled() -> bool:
    """Tracing only turns on when an endpoint is configured."""
    if not _env_flag('HIPPO_OTEL_ENABLED', True):
        return False
    return bool(otlp_endpoint())


def _sample_ratio() -> float:
    raw = os.environ.get('HIPPO_OTEL_SAMPLE_RATIO', str(_DEFAULT_SAMPLE_RATIO)).strip()
    try:
        ratio = float(raw)
    except ValueError:
        return _DEFAULT_SAMPLE_RATIO
    return min(max(ratio, 0.0), 1.0)


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

    Returns ``None`` when no OTLP endpoint is configured.
    """
    if not telemetry_enabled():
        return None

    resource = Resource.create(
        {
            'service.name': service_name,
            'service.version': _service_version(),
            'deployment.environment': os.environ.get('HIPPO_ENV', 'prod'),
            'host.name': socket.gethostname(),
        }
    )

    sampler = ParentBased(TraceIdRatioBased(_sample_ratio()))
    tracer_provider = TracerProvider(resource=resource, sampler=sampler)
    tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(tracer_provider)

    metric_reader = PeriodicExportingMetricReader(OTLPMetricExporter(), export_interval_millis=30_000)
    meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])
    metrics.set_meter_provider(meter_provider)

    logger_provider = LoggerProvider(resource=resource)
    logger_provider.add_log_record_processor(BatchLogRecordProcessor(OTLPLogExporter()))
    log_handler = LoggingHandler(level=logging.NOTSET, logger_provider=logger_provider)

    # capture_parameters stays off: statement parameters would leak account ids
    # and any credential that happens to travel through SQL.
    PsycopgInstrumentor().instrument(tracer_provider=tracer_provider, capture_parameters=False)
    HTTPXClientInstrumentor().instrument(tracer_provider=tracer_provider)
    # Without this the logging instrumentation skips stamping otelTraceID /
    # otelSpanID on each record, and stdlib log lines would lose their
    # correlation ids once they cross to the listener thread.
    os.environ.setdefault('OTEL_PYTHON_LOG_CORRELATION', 'true')
    LoggingInstrumentor().instrument(set_logging_format=False, tracer_provider=tracer_provider)
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


def add_span_attributes(attributes: dict[str, Any]) -> None:
    """Attach attributes to the active span, if there is one."""
    span = trace.get_current_span()
    if span is not None and span.is_recording():
        for key, value in attributes.items():
            span.set_attribute(key, value)


__all__ = [
    'TelemetryRuntime',
    'add_span_attributes',
    'get_meter',
    'get_tracer',
    'init_telemetry',
    'instrument_app',
    'otlp_endpoint',
    'telemetry_enabled',
]
