"""Logging entry points.

The pipeline itself lives in :mod:`hippo.observability.logging`; this module
keeps the historical ``setup_logger`` / ``get_logger`` names working for the CLI
and the workers, and supplies the level and file target from ``config.toml``.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

from .config import CLI_SERVICE, get_settings
from .observability.logging import configure_logging, get_logger, resolve_level

__all__ = ['configure_logging', 'get_logger', 'setup_logger']


def setup_logger(
    name: str = CLI_SERVICE,
    service: str = CLI_SERVICE,
    *,
    verbose: bool = False,
    extra_handlers: Sequence[logging.Handler] | None = None,
) -> logging.Logger:
    """Configure the process-wide logging pipeline and return a logger.

    Args:
        name: Logger name to hand back.
        service: Service name substituted into the configured log file path, so
            two processes never rotate the same file.
        verbose: Log at DEBUG instead of the configured level.
        extra_handlers: Additional sink handlers (e.g. the OTLP log exporter).

    Returns:
        The stdlib logger with the configured pipeline attached.
    """
    settings = get_settings().logging
    configure_logging(
        level=resolve_level(settings.level),
        verbose=verbose,
        json_output=settings.format != 'console',
        log_file=settings.file_for(service),
        extra_handlers=extra_handlers,
    )
    return logging.getLogger(name)
