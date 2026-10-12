"""Application configuration.

Everything deployment-specific lives in ``config.toml``: one file per machine,
never deployed, never in the environment. Code reads it through
:func:`get_settings` instead of touching ``os.environ``, so every value has
exactly one home and one name.

The file is resolved against the working directory, like the repository's other
conventions, because the services and the CLI all run from the repository root.
"""

from __future__ import annotations

import sys
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Final

CONFIG_FILE: Final = 'config.toml'

#: Service names, also the ``{service}`` substitution in log file paths.
WEB_SERVICE: Final = 'hippo-web'
WORKER_SERVICE: Final = 'hippo-sync-worker'
CLI_SERVICE: Final = 'hippo'

# ---------------------------------------------------------------- UI defaults
# Not deployment settings: these are shipped defaults the CLI and the parsers
# rely on, with no per-machine reason to differ.

APP_NAME: Final = 'hippo'
CLI_NAME: Final = 'hippo'
DEFAULT_HOST: Final = '127.0.0.1'
DEFAULT_PORT: Final = 8000
DEFAULT_USER_AGENT: Final = (
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
    'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36'
)
WECHAT_PROFILE_ENDPOINT: Final = 'https://mp.weixin.qq.com/mp/profile_ext'
WECHAT_COMMENT_ENDPOINT: Final = 'https://mp.weixin.qq.com/mp/appmsg_comment'
DEFAULT_PAGE_SIZE: Final = 10
# 列表阶段账号之间的请求间隔：searchcontact / 列表接口的限流底线（代码里还有下限兜底）
DEFAULT_SYNC_REQUEST_INTERVAL: Final = 15.0
DEFAULT_GROUP_NAME: Final = 'Default'
DEFAULT_RECENT_DAYS: Final = 7
DEFAULT_MAX_CONTENT_DOWNLOAD_ATTEMPTS: Final = 3
DEFAULT_WEIXIN_SDK_PATH: Final = '/opt/weixin-rs/sdk/python'


@dataclass(frozen=True)
class DatabaseSettings:
    """``[database]``."""

    dsn: str = ''
    pool_min: int = 1
    pool_max: int = 8
    #: Feed the tokenizer a query at startup so the first search is not slow.
    warmup_jieba: bool = True
    warmup_text: str = 'hippo'
    #: PostgreSQL's JIT hurts the big article queries, so it defaults to off.
    disable_jit: bool = True
    #: Log the schema initialization steps.
    init_log: bool = False

    def __post_init__(self) -> None:
        if self.pool_min < 1:
            object.__setattr__(self, 'pool_min', 1)
        if self.pool_max < self.pool_min:
            object.__setattr__(self, 'pool_max', self.pool_min)


@dataclass(frozen=True)
class S3Settings:
    """``[s3]``: an S3-compatible object store for article images."""

    endpoint: str = ''
    bucket: str = ''
    access_key: str = ''
    secret_key: str = ''
    region: str = 'us-east-1'
    prefix: str = ''

    @property
    def configured(self) -> bool:
        """Image storage is optional; a half-filled section counts as absent."""
        return bool(self.endpoint and self.bucket and self.access_key and self.secret_key)


@dataclass(frozen=True)
class WebSettings:
    """``[web]``."""

    #: ``Secure`` must be off while serving plain HTTP in development.
    cookie_secure: bool = False
    session_ttl_hours: float = 24 * 7
    #: Run the sync loops inside the web process. Development only.
    inprocess_sync: bool = False
    signoz_url: str = ''

    def __post_init__(self) -> None:
        if self.session_ttl_hours < 1:
            object.__setattr__(self, 'session_ttl_hours', 1.0)


@dataclass(frozen=True)
class LoggingSettings:
    """``[logging]``."""

    level: str = 'WARNING'
    #: ``json`` (what SignOz ingests) or ``console``.
    format: str = 'json'
    #: Rotating file target. ``{service}`` keeps the web app and the worker off
    #: each other's file, since two processes must not rotate the same one.
    #: Empty means no file sink.
    file: str = ''
    #: Trailing lines returned by the admin console's log tail.
    tail_lines: int = 500

    def file_for(self, service: str) -> str | None:
        return self.file.replace('{service}', service) if self.file else None


@dataclass(frozen=True)
class HttpSettings:
    """``[http]``: outbound proxying and the Cloudflare Worker relay."""

    proxy: str = ''
    article_worker: str = ''
    article_worker_proxy: str = ''
    #: Concurrent connections through the relay; unset means httpx's own cap.
    article_max_connections: int | None = None


@dataclass(frozen=True)
class WeixinSettings:
    """``[weixin]``: the weixin-rs daemon this app reads articles through."""

    sdk_path: str = DEFAULT_WEIXIN_SDK_PATH
    daemon_host: str = '127.0.0.1'
    daemon_port: int = 9099


@dataclass(frozen=True)
class ObservabilitySettings:
    """``[observability]``: OTLP export to a collector (SignOz)."""

    #: Telemetry only runs when this is on *and* an endpoint is configured.
    enabled: bool = True
    otlp_endpoint: str = ''
    #: Plaintext gRPC (``http://`` collectors) instead of TLS.
    insecure: bool = False
    #: 1.0 keeps every span, failed traces included. Any lower value drops
    #: whole traces at the SDK, errors and all: to trim volume, sample in the
    #: collector instead.
    sample_ratio: float = 1.0
    environment: str = 'prod'

    def __post_init__(self) -> None:
        object.__setattr__(self, 'sample_ratio', min(max(self.sample_ratio, 0.0), 1.0))


@dataclass(frozen=True)
class Settings:
    """The whole file."""

    database: DatabaseSettings = field(default_factory=DatabaseSettings)
    s3: S3Settings = field(default_factory=S3Settings)
    web: WebSettings = field(default_factory=WebSettings)
    logging: LoggingSettings = field(default_factory=LoggingSettings)
    http: HttpSettings = field(default_factory=HttpSettings)
    weixin: WeixinSettings = field(default_factory=WeixinSettings)
    observability: ObservabilitySettings = field(default_factory=ObservabilitySettings)


def _coerce(default: Any, raw: Any) -> Any:
    """Convert one TOML value to the type its field declares, via the default."""
    try:
        if default is None:
            text = str(raw).strip()
            return int(text) if text else None
        if isinstance(default, bool):
            return bool(raw)
        if isinstance(default, int):
            return int(raw)
        if isinstance(default, float):
            return float(raw)
        return str(raw).strip() or default
    except (TypeError, ValueError):
        return default


def _section[T](cls: type[T], table: Any) -> T:
    """Build one settings section from its TOML table, missing keys excepted."""
    if not isinstance(table, dict):
        return cls()
    defaults = cls()  # type: ignore[call-arg]
    values = {
        spec.name: _coerce(getattr(defaults, spec.name), table[spec.name])
        for spec in fields(defaults)  # type: ignore[arg-type]
        if spec.name in table
    }
    return cls(**values)  # type: ignore[call-arg]


def _read_toml(path: Path | str) -> dict[str, Any]:
    """Read the config file, reporting instead of raising when it is unusable.

    A missing file is normal (telemetry off, defaults everywhere). A malformed
    one is a mistake worth printing, but not worth refusing to start over.
    """
    file = Path(path)
    if not file.is_file():
        return {}
    try:
        return tomllib.loads(file.read_text(encoding='utf-8'))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        sys.stderr.write(f'Failed to read {file}: {exc}\n')
        return {}


def load_settings(path: Path | str = CONFIG_FILE) -> Settings:
    """Read every section from ``config.toml``."""
    raw = _read_toml(path)
    return Settings(
        database=_section(DatabaseSettings, raw.get('database')),
        s3=_section(S3Settings, raw.get('s3')),
        web=_section(WebSettings, raw.get('web')),
        logging=_section(LoggingSettings, raw.get('logging')),
        http=_section(HttpSettings, raw.get('http')),
        weixin=_section(WeixinSettings, raw.get('weixin')),
        observability=_section(ObservabilitySettings, raw.get('observability')),
    )


_SETTINGS: Settings | None = None


def get_settings() -> Settings:
    """Return the process-wide settings, reading the file on first use."""
    global _SETTINGS
    if _SETTINGS is None:
        _SETTINGS = load_settings()
    return _SETTINGS


def reset_settings() -> None:
    """Forget the cached settings so the next read hits the file again."""
    global _SETTINGS
    _SETTINGS = None


__all__ = [
    'APP_NAME',
    'CLI_NAME',
    'CLI_SERVICE',
    'CONFIG_FILE',
    'DEFAULT_GROUP_NAME',
    'DEFAULT_HOST',
    'DEFAULT_MAX_CONTENT_DOWNLOAD_ATTEMPTS',
    'DEFAULT_PAGE_SIZE',
    'DEFAULT_PORT',
    'DEFAULT_RECENT_DAYS',
    'DEFAULT_SYNC_REQUEST_INTERVAL',
    'DEFAULT_USER_AGENT',
    'DEFAULT_WEIXIN_SDK_PATH',
    'WEB_SERVICE',
    'WECHAT_COMMENT_ENDPOINT',
    'WECHAT_PROFILE_ENDPOINT',
    'WORKER_SERVICE',
    'DatabaseSettings',
    'HttpSettings',
    'LoggingSettings',
    'ObservabilitySettings',
    'S3Settings',
    'Settings',
    'WebSettings',
    'WeixinSettings',
    'get_settings',
    'load_settings',
    'reset_settings',
]
