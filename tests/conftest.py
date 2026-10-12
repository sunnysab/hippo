"""Shared test setup."""

from __future__ import annotations

import pytest

from hippo import config


@pytest.fixture(autouse=True)
def _default_settings(monkeypatch):
    """Never let a developer's config.toml leak into the suite.

    Tests that want settings read them from a file they wrote themselves.
    """
    config.reset_settings()
    monkeypatch.setattr(config, '_SETTINGS', config.Settings())
