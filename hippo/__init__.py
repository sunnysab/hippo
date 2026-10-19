"""Hippo: a self-hosted personal information and knowledge platform."""

from importlib.metadata import PackageNotFoundError, version

__all__ = ['__version__']

try:
    # pyproject.toml owns the version; reading it back from the installed
    # distribution is what keeps the package, /docs and the release in step.
    __version__ = version('hippo')
except PackageNotFoundError:  # pragma: no cover - a source tree that was never installed
    __version__ = '0.0.0+unknown'
