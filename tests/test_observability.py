"""Observability wiring: OTLP opt-in, span helpers, log handlers and redaction.

Telemetry must stay silent unless an OTLP endpoint is configured: the CLI, the
test suite and local development all run without a collector.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from hippo.observability import logging as obs_logging
from hippo.observability import metrics as metrics_module
from hippo.observability import otel as otel_module


def test_telemetry_is_off_without_endpoint(monkeypatch):
    monkeypatch.delenv('OTEL_EXPORTER_OTLP_ENDPOINT', raising=False)
    monkeypatch.delenv('OTEL_EXPORTER_OTLP_TRACES_ENDPOINT', raising=False)
    monkeypatch.delenv('HIPPO_OTEL_ENABLED', raising=False)

    assert otel_module.otlp_endpoint() is None
    assert otel_module.telemetry_enabled() is False


def test_telemetry_flag_overrides_configured_endpoint(monkeypatch):
    monkeypatch.setenv('OTEL_EXPORTER_OTLP_ENDPOINT', 'http://127.0.0.1:4317')

    monkeypatch.setenv('HIPPO_OTEL_ENABLED', '0')
    assert otel_module.telemetry_enabled() is False

    monkeypatch.setenv('HIPPO_OTEL_ENABLED', '1')
    assert otel_module.telemetry_enabled() is True


@pytest.mark.parametrize(
    ('raw', 'expected'),
    [(None, 0.1), ('0.5', 0.5), ('2', 1.0), ('-1', 0.0), ('oops', 0.1)],
)
def test_sample_ratio_is_clamped(monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv('HIPPO_OTEL_SAMPLE_RATIO', raising=False)
    else:
        monkeypatch.setenv('HIPPO_OTEL_SAMPLE_RATIO', raw)

    assert otel_module._sample_ratio() == expected


@pytest.fixture
def exported_spans(monkeypatch):
    """Install an in-memory tracer provider so spans can be inspected."""
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(trace, '_TRACER_PROVIDER', provider)
    return exporter


def test_span_records_name_and_skips_none_attributes(exported_spans):
    with otel_module.span('worker.drain', article_count=3, missing=None) as current:
        assert current.is_recording()

    (span,) = exported_spans.get_finished_spans()
    assert span.name == 'worker.drain'
    assert span.attributes['article_count'] == 3
    assert 'missing' not in span.attributes


def test_traced_marks_failed_spans_as_errors(exported_spans):
    from opentelemetry.trace import StatusCode

    @otel_module.traced('job.run', trigger='scheduled')
    async def boom() -> None:
        raise ValueError('nope')

    with pytest.raises(ValueError):
        asyncio.run(boom())

    (span,) = exported_spans.get_finished_spans()
    assert span.name == 'job.run'
    assert span.attributes['trigger'] == 'scheduled'
    assert span.status.status_code is StatusCode.ERROR


def test_traced_rejects_sync_functions():
    with pytest.raises(TypeError):
        otel_module.traced('sync.thing')(lambda: None)


def test_metric_instruments_are_cached_and_keyed_by_kind():
    metrics_module.reset_cache()
    try:
        counter = metrics_module.instrument('counter', 'hippo.test.count')
        assert metrics_module.instrument('counter', 'hippo.test.count') is counter
        assert metrics_module.instrument('histogram', 'hippo.test.count') is not counter
    finally:
        metrics_module.reset_cache()


def test_redact_masks_credential_keys_case_insensitively():
    event = {'password': 'hunter2', 'safe': 1, 'Authorization': 'Bearer x'}

    assert obs_logging._redact(None, 'info', dict(event)) == {
        'password': '***',
        'safe': 1,
        'Authorization': '***',
    }


def test_extra_handlers_are_fanned_out_on_the_listener_thread(monkeypatch):
    """Without this the worker would log to its file only, never to OTLP."""
    monkeypatch.delenv('HIPPO_LOG_FILE', raising=False)
    handler = logging.NullHandler()

    obs_logging.configure_logging(extra_handlers=[handler])
    try:
        assert obs_logging._LISTENER is not None
        assert handler in obs_logging._LISTENER.handlers
    finally:
        obs_logging.shutdown_logging()
