"""Logging entry points.

The pipeline itself lives in :mod:`hippo.observability.logging`; this module
keeps the historical ``setup_logger`` / ``get_logger`` names working for the CLI
and the workers.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Sequence

from .observability.logging import configure_logging, get_logger

__all__ = ['configure_logging', 'get_logger', 'setup_logger']


def setup_logger(
    name: str = 'hippo',
    level: int = logging.WARNING,
    verbose: bool = False,
    log_file: str | None = None,
    extra_handlers: Sequence[logging.Handler] | None = None,
) -> logging.Logger:
    """Configure the process-wide logging pipeline and return a logger.

    Args:
        name: Logger name to hand back.
        level: Console level when ``verbose`` is off.
        verbose: Log at DEBUG instead of ``level``.
        log_file: Rotating log file; falls back to ``HIPPO_LOG_FILE``.
        extra_handlers: Additional sink handlers (e.g. the OTLP log exporter).

    Returns:
        The stdlib logger with the configured pipeline attached.
    """
    configure_logging(
        level=level,
        verbose=verbose,
        log_file=log_file or os.environ.get('HIPPO_LOG_FILE'),
        extra_handlers=extra_handlers,
    )
    return logging.getLogger(name)
