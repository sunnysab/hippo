"""Observability wiring: OTLP opt-in, span helpers, handlers and redaction.

Telemetry must stay silent unless ``config.toml`` names an OTLP endpoint: the
CLI, the test suite and local development all run without a collector.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import pytest

from hippo import config
from hippo.observability import logging as obs_logging
from hippo.observability import metrics as metrics_module
from hippo.observability import otel as otel_module


@pytest.fixture(autouse=True)
def _fresh_settings():
    """Every test reads the config file from its own working directory."""
    config.reset_settings()
    yield
    config.reset_settings()


def _write_config(tmp_path: Path, body: str) -> None:
    (tmp_path / 'config.toml').write_text(body, encoding='utf-8')


def test_telemetry_is_off_without_a_config_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    assert otel_module.otlp_endpoint() is None
    assert otel_module.telemetry_enabled() is False


def test_telemetry_reads_the_endpoint_from_config_toml(tmp_path, monkeypatch):
    _write_config(tmp_path, '[observability]\notlp_endpoint = "http://collector:4317"\ninsecure = true\n')
    monkeypatch.chdir(tmp_path)

    assert otel_module.telemetry_enabled() is True
    assert otel_module.otlp_endpoint() == 'http://collector:4317'


def test_telemetry_can_be_switched_off_without_losing_the_endpoint(tmp_path, monkeypatch):
    _write_config(tmp_path, '[observability]\nenabled = false\notlp_endpoint = "http://collector:4317"\n')
    monkeypatch.chdir(tmp_path)

    assert otel_module.telemetry_enabled() is False
    assert otel_module.otlp_endpoint() == 'http://collector:4317'


def test_exporters_take_the_endpoint_from_the_file(monkeypatch):
    """The SDK would otherwise fall back to OTEL_EXPORTER_OTLP_ENDPOINT."""
    captured: list[dict[str, object]] = []

    class _FakeExporter:
        def __init__(self, **kwargs: object) -> None:
            captured.append(kwargs)

    for name in ('OTLPSpanExporter', 'OTLPMetricExporter', 'OTLPLogExporter'):
        monkeypatch.setattr(otel_module, name, _FakeExporter)

    otel_module._exporters(config.ObservabilitySettings(otlp_endpoint='http://collector:4317'))

    assert captured == [{'endpoint': 'http://collector:4317', 'insecure': False}] * 3


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


def test_setup_logger_uses_the_configured_level_and_file(tmp_path, monkeypatch):
    _write_config(tmp_path, '[logging]\nlevel = "DEBUG"\nfile = "logs/{service}.log"\n')
    monkeypatch.chdir(tmp_path)
    handler = logging.NullHandler()

    from hippo.logger import setup_logger

    setup_logger(service=config.WORKER_SERVICE, extra_handlers=[handler])
    try:
        assert obs_logging._LISTENER is not None
        assert handler in obs_logging._LISTENER.handlers
        assert logging.getLogger().level == logging.DEBUG
        assert (tmp_path / 'logs' / 'hippo-sync-worker.log').exists()
    finally:
        obs_logging.shutdown_logging()
