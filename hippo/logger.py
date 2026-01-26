"""Logging configuration for the CLI."""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys


def setup_logger(
    name: str = "hippo",
    level: int = logging.WARNING,
    verbose: bool = False,
    log_file: str | None = None,
) -> logging.Logger:
    """Configure and return a logger instance.
    
    Args:
        name: Logger name
        level: Logging level for console output (default: WARNING)
        verbose: If True, set console level to DEBUG
        log_file: Path to log file. If None, checks HIPPO_LOG_FILE env var.
        
    Returns:
        Configured logger instance
    """
    logger = logging.getLogger(name)
    
    # Avoid duplicate handlers if called multiple times
    if logger.handlers:
        return logger
    
    # Set root logger to lowest level we care about (INFO) so handlers can filter
    logger.setLevel(logging.INFO)
    
    # Console Handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_level = logging.DEBUG if verbose else level
    console_handler.setLevel(console_level)
    console_formatter = logging.Formatter(
        "%(levelname)s: %(message)s"
    )
    console_handler.setFormatter(console_formatter)
    logger.addHandler(console_handler)

    # File Handler
    resolved_log_file = log_file or os.environ.get("HIPPO_LOG_FILE")
    if resolved_log_file:
        try:
            # Ensure directory exists
            log_dir = os.path.dirname(os.path.abspath(resolved_log_file))
            if log_dir and not os.path.exists(log_dir):
                os.makedirs(log_dir, exist_ok=True)

            file_handler = logging.handlers.TimedRotatingFileHandler(
                resolved_log_file,
                when="midnight",
                interval=1,
                backupCount=7,
                encoding="utf-8"
            )
            file_handler.setLevel(logging.INFO)
            file_formatter = logging.Formatter(
                "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
            )
            file_handler.setFormatter(file_formatter)
            logger.addHandler(file_handler)
        except Exception as e:
            # Fallback to console if file logging fails
            sys.stderr.write(f"Failed to setup file logging: {e}\n")

    return logger


def get_logger(name: str = "hippo") -> logging.Logger:
    """Get or create logger instance.
    
    Args:
        name: Logger name, can use module path like 'hippo.http'
        
    Returns:
        Logger instance
    """
    return logging.getLogger(name)


__all__ = ["setup_logger", "get_logger"]
