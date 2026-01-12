"""Configuration helpers for the CLI."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Final

from platformdirs import user_data_dir

APP_NAME: Final = "wechat-article-exporter"
CLI_NAME: Final = "wechatcli"
DEFAULT_USER_AGENT: Final = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/117.0.0.0 Safari/537.36 WAE/1.0"
)
WECHAT_PROFILE_ENDPOINT: Final = "https://mp.weixin.qq.com/mp/profile_ext"
WECHAT_COMMENT_ENDPOINT: Final = "https://mp.weixin.qq.com/mp/appmsg_comment"
DEFAULT_PAGE_SIZE: Final = 10


def _resolve_home() -> Path:
    override = os.environ.get("WECHATCLI_HOME")
    if override:
        return Path(override).expanduser().resolve()
    return Path(user_data_dir(appname=CLI_NAME, appauthor=APP_NAME))


def _env_int(name: str, default: int | None = None) -> int | None:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


HOME_DIR: Final = _resolve_home()
DB_PATH: Final = HOME_DIR / "cli.db"
DOWNLOAD_ROOT: Final = HOME_DIR / "downloads"
LOG_PATH: Final = HOME_DIR / "cli.log"
ARTICLE_WORKER_URL: Final = os.environ.get("WECHATCLI_ARTICLE_WORKER")
ARTICLE_WORKER_PROXY: Final = os.environ.get("WECHATCLI_ARTICLE_WORKER_PROXY")
ARTICLE_WORKER_MAX_CONNECTIONS: Final = _env_int("WECHATCLI_ARTICLE_MAX_CONNECTIONS")

__all__ = [
    "APP_NAME",
    "CLI_NAME",
    "DEFAULT_USER_AGENT",
    "WECHAT_PROFILE_ENDPOINT",
    "WECHAT_COMMENT_ENDPOINT",
    "DEFAULT_PAGE_SIZE",
    "HOME_DIR",
    "DB_PATH",
    "DOWNLOAD_ROOT",
    "LOG_PATH",
    "ARTICLE_WORKER_URL",
    "ARTICLE_WORKER_PROXY",
    "ARTICLE_WORKER_MAX_CONNECTIONS",
]
