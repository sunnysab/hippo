"""Lazily created OpenTelemetry instruments.

Instruments have to be created after the meter provider is installed, so they
are built on first use and cached instead of being module-level constants.
"""

from __future__ import annotations

from typing import Any

from .otel import get_meter

_METER_NAME = 'hippo'
_CACHE: dict[str, Any] = {}


def instrument(kind: str, name: str, **kwargs: Any) -> Any:
    """Return a cached instrument, creating it on first use.

    Args:
        kind: ``counter``, ``histogram``, ``updowncounter`` or ``gauge``.
        name: Metric name, e.g. ``hippo.article.drained``.
        **kwargs: Passed through to the meter factory (``unit``, ``description``).

    Returns:
        The OpenTelemetry instrument.
    """
    key = f'{kind}:{name}'
    if key not in _CACHE:
        meter = get_meter(_METER_NAME)
        factory = getattr(meter, f'create_{kind}')
        _CACHE[key] = factory(name, **kwargs)
    return _CACHE[key]


def reset_cache() -> None:
    """Drop cached instruments (used by tests)."""
    _CACHE.clear()


__all__ = ['instrument', 'reset_cache']
