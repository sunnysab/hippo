"""Configuration helpers for the CLI."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Final, Optional

from platformdirs import user_data_dir

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

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
CONFIG_DIR: Final = HOME_DIR
PROFILE_PATH: Final = CONFIG_DIR / "profiles.toml"


def load_profile(profile_name: str = "default") -> dict[str, Any]:
    """Load settings from profile configuration file.
    
    Args:
        profile_name: Name of the profile to load
        
    Returns:
        Dictionary with profile settings
        
    Raises:
        FileNotFoundError: If profiles.toml doesn't exist
        ValueError: If profile_name not found in config
    """
    if not PROFILE_PATH.exists():
        raise FileNotFoundError(f"Profile config not found: {PROFILE_PATH}")
    
    with open(PROFILE_PATH, "rb") as f:
        config = tomllib.load(f)
    
    if "profiles" not in config:
        raise ValueError("No [profiles] section in config file")
    
    profiles = config["profiles"]
    if profile_name not in profiles:
        available = ", ".join(profiles.keys())
        raise ValueError(
            f"Profile '{profile_name}' not found. Available: {available}"
        )
    
    return profiles[profile_name]


def get_profile_value(profile: dict[str, Any], key: str, default: Any = None) -> Any:
    """Get value from profile with fallback to default."""
    return profile.get(key, default)

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
    "CONFIG_DIR",
    "PROFILE_PATH",
    "load_profile",
    "get_profile_value",
]
