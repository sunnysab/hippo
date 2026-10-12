"""Application configuration.

Secrets and per-host facts live in ``.env`` / the environment. Deployment
settings that belong in version control live in ``config.toml``.
"""

from __future__ import annotations

import os
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from dotenv import load_dotenv

load_dotenv()

#: Resolved against the working directory, like ``.env``: the services and the
#: CLI all run from the repository root.
CONFIG_FILE: Final = 'config.toml'

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


def _env_int(name: str, default: int | None = None) -> int | None:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


@dataclass(frozen=True)
class ObservabilitySettings:
    """The ``[observability]`` table of ``config.toml``."""

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


def _sample_ratio(value: object, default: float) -> float:
    try:
        ratio = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return min(max(ratio, 0.0), 1.0)


def load_observability_settings(path: Path | str = CONFIG_FILE) -> ObservabilitySettings:
    """Read the observability settings, defaulting to "telemetry off".

    Telemetry is optional, so a missing or malformed file must not stop the app;
    it is reported on stderr and the defaults are used instead.
    """
    file = Path(path)
    if not file.is_file():
        return ObservabilitySettings()
    try:
        raw = tomllib.loads(file.read_text(encoding='utf-8'))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        sys.stderr.write(f'Failed to read {file}: {exc}\n')
        return ObservabilitySettings()
    table = raw.get('observability')
    if not isinstance(table, dict):
        return ObservabilitySettings()
    defaults = ObservabilitySettings()
    environment = str(table.get('environment') or '').strip()
    return ObservabilitySettings(
        enabled=bool(table.get('enabled', defaults.enabled)),
        otlp_endpoint=str(table.get('otlp_endpoint') or '').strip(),
        insecure=bool(table.get('insecure', defaults.insecure)),
        sample_ratio=_sample_ratio(table.get('sample_ratio'), defaults.sample_ratio),
        environment=environment or defaults.environment,
    )


ARTICLE_WORKER_URL: Final = os.environ.get('HIPPO_ARTICLE_WORKER')
ARTICLE_WORKER_PROXY: Final = os.environ.get('HIPPO_ARTICLE_WORKER_PROXY')
ARTICLE_WORKER_MAX_CONNECTIONS: Final = _env_int('HIPPO_ARTICLE_MAX_CONNECTIONS')
HTTP_PROXY: Final = os.environ.get('HIPPO_HTTP_PROXY')

__all__ = [
    'APP_NAME',
    'ARTICLE_WORKER_MAX_CONNECTIONS',
    'ARTICLE_WORKER_PROXY',
    'ARTICLE_WORKER_URL',
    'CLI_NAME',
    'CONFIG_FILE',
    'DEFAULT_GROUP_NAME',
    'DEFAULT_HOST',
    'DEFAULT_MAX_CONTENT_DOWNLOAD_ATTEMPTS',
    'DEFAULT_PAGE_SIZE',
    'DEFAULT_PORT',
    'DEFAULT_RECENT_DAYS',
    'DEFAULT_SYNC_REQUEST_INTERVAL',
    'DEFAULT_USER_AGENT',
    'HTTP_PROXY',
    'WECHAT_COMMENT_ENDPOINT',
    'WECHAT_PROFILE_ENDPOINT',
    'ObservabilitySettings',
    'load_observability_settings',
]


#: SignOz front-end base URL. When set, the admin log page deep-links into it.
SIGNOZ_URL = os.environ.get('HIPPO_SIGNOZ_URL', '').rstrip('/')

#: How many trailing lines the fallback log tail returns.
LOG_TAIL_LINES = int(os.environ.get('HIPPO_LOG_TAIL_LINES', '500'))
