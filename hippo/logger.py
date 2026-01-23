"""Logging configuration for the CLI."""

from __future__ import annotations

import logging
import sys


def setup_logger(
    name: str = "hippo",
    level: int = logging.INFO,
    verbose: bool = False,
) -> logging.Logger:
    """Configure and return a logger instance.
    
    Args:
        name: Logger name
        level: Logging level for console output
        verbose: If True, also output detailed logs to console
        
    Returns:
        Configured logger instance
    """
    logger = logging.getLogger(name)
    
    # Avoid duplicate handlers if called multiple times
    if logger.handlers:
        return logger
    
    logger.setLevel(logging.DEBUG)
    
    if verbose:
        console_handler = logging.StreamHandler(sys.stderr)
        console_handler.setLevel(level)
        console_formatter = logging.Formatter(
            "%(levelname)s: %(message)s"
        )
        console_handler.setFormatter(console_formatter)
        logger.addHandler(console_handler)
    
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
