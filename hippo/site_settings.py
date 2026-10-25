"""Site-wide settings stored in ``meta``.

Scalar knobs that belong to the instance rather than to a user. Keeping them in
the existing key/value table avoids a one-row table for three fields.
"""

from __future__ import annotations

from typing import Any

from .exceptions import ApiError
from .storage import PostgresStorage, load_meta_json, save_meta_json
from .topics import default_vocabulary, validate_vocabulary

SITE_SETTINGS_KEY = 'site:settings'

_DEFAULT_SITE_NAME = 'Hippo'

#: Keys ``set_site_settings`` will write. Anything else in the update is ignored,
#: so a crafted PATCH cannot smuggle in a field the readers never expect.
_WRITABLE = ('registration_enabled', 'site_name', 'public_base_url', 'vocabulary')


def default_site_settings() -> dict[str, Any]:
    return {
        # Off by default: an instance should not accept sign-ups until an admin
        # has configured SMTP and decided to open registration.
        'registration_enabled': False,
        'site_name': _DEFAULT_SITE_NAME,
        # Absolute base URL used to build links inside e-mails.
        'public_base_url': '',
        # Direction vocabulary for article classification. Its ``version`` makes
        # every cached classification stale, so editing it here is what triggers
        # the requeue.
        'vocabulary': default_vocabulary(),
    }


async def get_site_settings(storage: PostgresStorage) -> dict[str, Any]:
    """Return the site settings, filling in defaults for missing keys."""
    stored = await load_meta_json(storage, SITE_SETTINGS_KEY, {})
    settings = default_site_settings()
    if isinstance(stored, dict):
        settings.update({k: v for k, v in stored.items() if k in settings})
    settings['registration_enabled'] = bool(settings['registration_enabled'])
    # A stored vocabulary an older build wrote is untrusted input: fall back to
    # the default rather than handing the classifier a shape it cannot read.
    try:
        settings['vocabulary'] = validate_vocabulary(settings['vocabulary'])
    except ApiError:
        settings['vocabulary'] = default_vocabulary()
    return settings


async def set_site_settings(storage: PostgresStorage, updates: dict[str, Any]) -> dict[str, Any]:
    """Merge ``updates`` into the stored site settings and return the result."""
    settings = await get_site_settings(storage)
    # Captured before the merge below overwrites it: the version bump compares the
    # submitted topic list against what is actually stored right now.
    stored_vocabulary = settings['vocabulary']
    for key in _WRITABLE:
        if key in updates:
            settings[key] = updates[key]
    if 'vocabulary' in updates:
        settings['vocabulary'] = _next_vocabulary(stored_vocabulary, updates['vocabulary'])
    settings['registration_enabled'] = bool(settings['registration_enabled'])
    settings['site_name'] = str(settings['site_name'] or _DEFAULT_SITE_NAME).strip() or _DEFAULT_SITE_NAME
    settings['public_base_url'] = str(settings['public_base_url'] or '').strip().rstrip('/')
    async with storage.transaction():
        await save_meta_json(storage, SITE_SETTINGS_KEY, settings)
    return settings


def _next_vocabulary(stored: dict[str, Any], submitted: Any) -> dict[str, Any]:
    """Validate an edited vocabulary and bump its version if it really changed.

    The version is the cache-invalidation switch, so it must move whenever the
    topic list does and stay put when it does not -- an unchanged save that bumped
    it would requeue every article for nothing.
    """
    vocabulary = validate_vocabulary(submitted)
    if vocabulary['topics'] == stored.get('topics'):
        vocabulary['version'] = int(stored.get('version') or 1)
        return vocabulary
    vocabulary['version'] = int(stored.get('version') or 1) + 1
    return vocabulary


__all__ = ['SITE_SETTINGS_KEY', 'default_site_settings', 'get_site_settings', 'set_site_settings']
