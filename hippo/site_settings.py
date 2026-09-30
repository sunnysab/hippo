"""Site-wide settings stored in ``meta``.

Scalar knobs that belong to the instance rather than to a user. Keeping them in
the existing key/value table avoids a one-row table for three fields.
"""

from __future__ import annotations

from typing import Any

from .storage import PostgresStorage, load_meta_json, save_meta_json

SITE_SETTINGS_KEY = 'site:settings'

_DEFAULT_SITE_NAME = 'Hippo'


def default_site_settings() -> dict[str, Any]:
    return {
        # Off by default: an instance should not accept sign-ups until an admin
        # has configured SMTP and decided to open registration.
        'registration_enabled': False,
        'site_name': _DEFAULT_SITE_NAME,
        # Absolute base URL used to build links inside e-mails.
        'public_base_url': '',
    }


async def get_site_settings(storage: PostgresStorage) -> dict[str, Any]:
    """Return the site settings, filling in defaults for missing keys."""
    stored = await load_meta_json(storage, SITE_SETTINGS_KEY, {})
    settings = default_site_settings()
    if isinstance(stored, dict):
        settings.update({k: v for k, v in stored.items() if k in settings})
    settings['registration_enabled'] = bool(settings['registration_enabled'])
    return settings


async def set_site_settings(storage: PostgresStorage, updates: dict[str, Any]) -> dict[str, Any]:
    """Merge ``updates`` into the stored site settings and return the result."""
    settings = await get_site_settings(storage)
    for key in ('registration_enabled', 'site_name', 'public_base_url'):
        if key in updates:
            settings[key] = updates[key]
    settings['registration_enabled'] = bool(settings['registration_enabled'])
    settings['site_name'] = str(settings['site_name'] or _DEFAULT_SITE_NAME).strip() or _DEFAULT_SITE_NAME
    settings['public_base_url'] = str(settings['public_base_url'] or '').strip().rstrip('/')
    async with storage.transaction():
        await save_meta_json(storage, SITE_SETTINGS_KEY, settings)
    return settings


__all__ = ['SITE_SETTINGS_KEY', 'default_site_settings', 'get_site_settings', 'set_site_settings']
