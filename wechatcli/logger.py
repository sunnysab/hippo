"""Logging configuration for the CLI."""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Optional

from .config import HOME_DIR, LOG_PATH


def setup_logger(
    name: str = "wechatcli",
    level: int = logging.INFO,
    log_file: Optional[Path] = None,
    verbose: bool = False,
) -> logging.Logger:
    """Configure and return a logger instance.
    
    Args:
        name: Logger name
        level: Logging level for file handler
        log_file: Path to log file, defaults to LOG_PATH
        verbose: If True, also output detailed logs to console
        
    Returns:
        Configured logger instance
    """
    logger = logging.getLogger(name)
    
    # Avoid duplicate handlers if called multiple times
    if logger.handlers:
        return logger
    
    logger.setLevel(logging.DEBUG)
    
    # Ensure log directory exists
    if log_file is None:
        log_file = LOG_PATH
    log_file.parent.mkdir(parents=True, exist_ok=True)
    
    # File handler - detailed logs with DEBUG level
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setLevel(level)
    file_formatter = logging.Formatter(
        "%(asctime)s | %(name)s | %(levelname)-8s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    file_handler.setFormatter(file_formatter)
    logger.addHandler(file_handler)
    
    # Console handler - only warnings and errors by default
    if verbose:
        console_handler = logging.StreamHandler(sys.stderr)
        console_handler.setLevel(logging.DEBUG)
        console_formatter = logging.Formatter(
            "%(levelname)s: %(message)s"
        )
        console_handler.setFormatter(console_formatter)
        logger.addHandler(console_handler)
    
    return logger


def get_logger(name: str = "wechatcli") -> logging.Logger:
    """Get or create logger instance.
    
    Args:
        name: Logger name, can use module path like 'wechatcli.http'
        
    Returns:
        Logger instance
    """
    return logging.getLogger(name)


__all__ = ["setup_logger", "get_logger"]
