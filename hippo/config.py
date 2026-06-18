"""Configuration helpers for the CLI."""

from __future__ import annotations

import os
from typing import Final

from .env import load_env

load_env()

APP_NAME: Final = 'hippo'
CLI_NAME: Final = 'hippo'
DEFAULT_USER_AGENT: Final = (
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
    'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/117.0.0.0 Safari/537.36 WAE/1.0'
)
WECHAT_PROFILE_ENDPOINT: Final = 'https://mp.weixin.qq.com/mp/profile_ext'
WECHAT_COMMENT_ENDPOINT: Final = 'https://mp.weixin.qq.com/mp/appmsg_comment'
DEFAULT_PAGE_SIZE: Final = 10
DEFAULT_GROUP_NAME: Final = 'Default'
DEFAULT_RECENT_DAYS: Final = 7
DEFAULT_WINDOW_START_HOUR: Final = 6
DEFAULT_WINDOW_END_HOUR: Final = 24


def _env_int(name: str, default: int | None = None) -> int | None:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


ARTICLE_WORKER_URL: Final = os.environ.get('HIPPO_ARTICLE_WORKER')
ARTICLE_WORKER_PROXY: Final = os.environ.get('HIPPO_ARTICLE_WORKER_PROXY')
ARTICLE_WORKER_MAX_CONNECTIONS: Final = _env_int('HIPPO_ARTICLE_MAX_CONNECTIONS')

__all__ = [
    'APP_NAME',
    'ARTICLE_WORKER_MAX_CONNECTIONS',
    'ARTICLE_WORKER_PROXY',
    'ARTICLE_WORKER_URL',
    'CLI_NAME',
    'DEFAULT_GROUP_NAME',
    'DEFAULT_PAGE_SIZE',
    'DEFAULT_RECENT_DAYS',
    'DEFAULT_USER_AGENT',
    'DEFAULT_WINDOW_END_HOUR',
    'DEFAULT_WINDOW_START_HOUR',
    'WECHAT_COMMENT_ENDPOINT',
    'WECHAT_PROFILE_ENDPOINT',
]
