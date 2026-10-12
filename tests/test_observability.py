"""Observability wiring: config.toml opt-in, span helpers, handlers and redaction.

Telemetry must stay silent unless ``config.toml`` names an OTLP endpoint: the
CLI, the test suite and local development all run without a collector.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import pytest

from hippo.config import ObservabilitySettings, load_observability_settings
from hippo.observability import logging as obs_logging
from hippo.observability import metrics as metrics_module
from hippo.observability import otel as otel_module


def _write_config(tmp_path: Path, body: str) -> Path:
    path = tmp_path / 'config.toml'
    path.write_text(body, encoding='utf-8')
    return path


def test_telemetry_is_off_without_a_config_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    assert otel_module.otlp_endpoint() is None
    assert otel_module.telemetry_enabled() is False


def test_telemetry_reads_the_endpoint_from_config_toml(tmp_path, monkeypatch):
    _write_config(tmp_path, '[observability]\notlp_endpoint = "http://collector:4317"\ninsecure = true\n')
    monkeypatch.chdir(tmp_path)

    assert otel_module.telemetry_enabled() is True
    assert otel_module.otlp_endpoint() == 'http://collector:4317'


def test_telemetry_can_be_switched_off_without_losing_the_endpoint(tmp_path):
    path = _write_config(
        tmp_path,
        '[observability]\nenabled = false\notlp_endpoint = "http://collector:4317"\n',
    )

    settings = load_observability_settings(path)
    assert settings.enabled is False
    assert settings.otlp_endpoint == 'http://collector:4317'


@pytest.mark.parametrize(('raw', 'expected'), [('0.5', 0.5), ('2', 1.0), ('-1', 0.0)])
def test_sample_ratio_is_clamped(tmp_path, raw, expected):
    path = _write_config(tmp_path, f'[observability]\nsample_ratio = {raw}\n')

    assert load_observability_settings(path).sample_ratio == expected


def test_an_invalid_sample_ratio_falls_back_to_the_default(tmp_path):
    path = _write_config(tmp_path, '[observability]\nsample_ratio = "oops"\n')

    assert load_observability_settings(path).sample_ratio == ObservabilitySettings().sample_ratio


def test_a_broken_file_falls_back_to_defaults(tmp_path, capsys):
    path = _write_config(tmp_path, 'not = [valid')

    assert load_observability_settings(path) == ObservabilitySettings()
    assert 'Failed to read' in capsys.readouterr().err


def test_exporters_take_the_endpoint_from_the_file(monkeypatch):
    """The SDK would otherwise fall back to OTEL_EXPORTER_OTLP_ENDPOINT."""
    captured: list[dict[str, object]] = []

    class _FakeExporter:
        def __init__(self, **kwargs: object) -> None:
            captured.append(kwargs)

    for name in ('OTLPSpanExporter', 'OTLPMetricExporter', 'OTLPLogExporter'):
        monkeypatch.setattr(otel_module, name, _FakeExporter)

    otel_module._exporters(ObservabilitySettings(otlp_endpoint='http://collector:4317'))

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
