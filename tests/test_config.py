"""config.toml is the single source for every deployment setting."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hippo import config


def _write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / 'config.toml'
    path.write_text(body, encoding='utf-8')
    return path


def test_a_missing_file_yields_the_defaults(tmp_path):
    assert config.load_settings(tmp_path / 'config.toml') == config.Settings()


def test_every_section_is_read(tmp_path):
    path = _write(
        tmp_path,
        """
[database]
dsn = "postgresql://db/hippo"
pool_min = 2
pool_max = 4
warmup_jieba = false
warmup_text = "hippo warm"
disable_jit = false
init_log = true

[s3]
endpoint = "http://minio:3900"
bucket = "hippo"
access_key = "key"
secret_key = "secret"
prefix = "custom/"

[web]
cookie_secure = true
session_ttl_hours = 12
inprocess_sync = true
signoz_url = "http://signoz/"

[logging]
level = "INFO"
format = "console"
file = "logs/{service}.log"
tail_lines = 100

[http]
article_worker = "https://worker.example"
article_max_connections = 4

[weixin]
sdk_path = "/opt/sdk"
daemon_port = 9999

[observability]
otlp_endpoint = "http://collector:4317"
insecure = true
sample_ratio = 0.25
environment = "staging"
""",
    )

    settings = config.load_settings(path)

    assert settings.database == config.DatabaseSettings(
        dsn='postgresql://db/hippo',
        pool_min=2,
        pool_max=4,
        warmup_jieba=False,
        warmup_text='hippo warm',
        disable_jit=False,
        init_log=True,
    )
    assert settings.s3 == config.S3Settings(
        endpoint='http://minio:3900',
        bucket='hippo',
        access_key='key',
        secret_key='secret',
        region='us-east-1',
        prefix='custom/',
    )
    assert settings.web == config.WebSettings(
        cookie_secure=True,
        session_ttl_hours=12,
        inprocess_sync=True,
        signoz_url='http://signoz/',
    )
    assert settings.logging.level == 'INFO'
    assert settings.logging.format == 'console'
    assert settings.logging.tail_lines == 100
    assert settings.http == config.HttpSettings(
        article_worker='https://worker.example',
        article_max_connections=4,
    )
    assert settings.weixin.sdk_path == '/opt/sdk'
    assert settings.weixin.daemon_host == '127.0.0.1'
    assert settings.weixin.daemon_port == 9999
    assert settings.observability == config.ObservabilitySettings(
        enabled=True,
        otlp_endpoint='http://collector:4317',
        insecure=True,
        sample_ratio=0.25,
        environment='staging',
    )


def test_log_file_paths_are_per_service(tmp_path):
    """Two processes must never rotate the same file."""
    path = _write(tmp_path, '[logging]\nfile = "logs/{service}.log"\n')

    settings = config.load_settings(path)

    assert settings.logging.file_for(config.WEB_SERVICE) == 'logs/hippo-web.log'
    assert settings.logging.file_for(config.WORKER_SERVICE) == 'logs/hippo-sync-worker.log'


def test_an_empty_log_file_means_no_file_sink(tmp_path):
    assert config.load_settings(tmp_path / 'config.toml').logging.file_for(config.WEB_SERVICE) is None


def test_a_half_filled_s3_section_counts_as_absent(tmp_path):
    path = _write(tmp_path, '[s3]\nendpoint = "http://minio:3900"\nbucket = "hippo"\n')

    assert config.load_settings(path).s3.configured is False


def test_bounds_are_clamped(tmp_path):
    path = _write(
        tmp_path,
        '[database]\npool_min = 0\npool_max = 1\n[web]\nsession_ttl_hours = 0.1\n[observability]\nsample_ratio = 5\n',
    )

    settings = config.load_settings(path)

    assert (settings.database.pool_min, settings.database.pool_max) == (1, 1)
    assert settings.web.session_ttl_hours == 1.0
    assert settings.observability.sample_ratio == 1.0


def test_an_invalid_sample_ratio_falls_back_to_the_default(tmp_path):
    path = _write(tmp_path, '[observability]\nsample_ratio = "oops"\n')

    assert config.load_settings(path).observability.sample_ratio == config.ObservabilitySettings().sample_ratio


def test_every_trace_is_exported_by_default(tmp_path):
    """A lower default would drop failed traces inside the SDK."""
    path = _write(tmp_path, '[observability]\notlp_endpoint = "http://collector:4317"\n')

    assert config.load_settings(path).observability.sample_ratio == 1.0


def test_unknown_keys_are_ignored(tmp_path):
    path = _write(tmp_path, '[database]\ndsn = "x"\nremoved_key = 1\n')

    assert config.load_settings(path).database.dsn == 'x'


def test_an_empty_value_falls_back_to_the_default(tmp_path):
    path = _write(tmp_path, '[weixin]\ndaemon_host = "   "\n')

    assert config.load_settings(path).weixin.daemon_host == '127.0.0.1'


def test_a_broken_file_falls_back_to_the_defaults(tmp_path, capsys):
    path = _write(tmp_path, 'not = [valid')

    assert config.load_settings(path) == config.Settings()
    assert 'Failed to read' in capsys.readouterr().err


def test_get_settings_caches_until_reset(tmp_path, monkeypatch):
    _write(tmp_path, '[database]\ndsn = "first"\n')
    monkeypatch.chdir(tmp_path)
    config.reset_settings()
    try:
        assert config.get_settings().database.dsn == 'first'

        _write(tmp_path, '[database]\ndsn = "second"\n')
        assert config.get_settings().database.dsn == 'first'

        config.reset_settings()
        assert config.get_settings().database.dsn == 'second'
    finally:
        config.reset_settings()


@pytest.mark.parametrize(
    ('raw', 'expected'),
    [
        (['http://localhost:5173'], ('http://localhost:5173',)),
        ([], ()),
        ('http://localhost:5173', ()),  # a scalar is malformed, not one origin
    ],
)
def test_allowed_origins_is_a_tuple_of_named_origins(tmp_path, monkeypatch, raw, expected) -> None:
    """CORS defaults to closed, and a malformed value must not open it."""
    from hippo import config

    path = tmp_path / 'config.toml'
    path.write_text(f'[web]\nallowed_origins = {json.dumps(raw)}\n', encoding='utf-8')
    monkeypatch.chdir(tmp_path)

    assert config.load_settings(path).web.allowed_origins == expected
