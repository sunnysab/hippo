"""Media source and transcription settings.

Instance-wide knobs for the video / podcast sources and the ASR backend. They
live in ``meta`` next to ``site:settings`` and ``sync:settings`` so an admin can
change them from the console without a redeploy.

Transcription talks to an OpenAI-compatible ``/v1/audio/transcriptions``. Two
tiers point at two independent servers: ``accurate`` is the default, because a
wrong transcript costs more than a slow one (see PLAN §1.7 for the measurements
behind that choice), while ``fast`` exists for bulk passes that a reader can
upgrade later with a re-transcribe.
"""

from __future__ import annotations

from typing import Any

from .storage import PostgresStorage, load_meta_json, save_meta_json

MEDIA_SETTINGS_KEY = 'media:settings'

#: Wire protocols hippo knows how to call. ``crispasr-server`` is what we run in
#: production; plain ``openai`` is kept for pointing at any compatible endpoint.
ASR_PROTOCOLS = ('crispasr-server', 'openai')

#: Tier names, cheapest-to-trust first. `accurate` is the default.
ASR_TIERS = ('accurate', 'fast')

TIER_ACCURATE = 'accurate'
TIER_FAST = 'fast'

_DEFAULT_ENDPOINTS: dict[str, str] = {
    TIER_ACCURATE: 'http://192.168.130.202:8317/v1',
    TIER_FAST: 'http://192.168.130.202:8318/v1',
}

#: Bounds keep a typo in the console from turning into a request that can never
#: finish, or an upload we would have to keep alive forever.
_CHUNK_SECONDS_RANGE = (30, 600)
#: A join of the last two chunks must fit, so this ceiling is allowed past the
#: chunk range; see `split()` in media/audio.py.
_MAX_CHUNK_SECONDS_RANGE = (30, 1200)
_TAIL_SECONDS_RANGE = (0, 300)
_CONCURRENCY_RANGE = (1, 4)
_ITEMS_PER_SYNC_RANGE = (1, 50)
_TIMEOUT_SECONDS_RANGE = (30, 3600)
_MAX_DURATION_SECONDS_RANGE = (60, 86400)
_ATTEMPTS_RANGE = (1, 10)

#: `original` keeps the downloaded file untouched; anything else is an ffmpeg
#: audio bitrate used when archiving.
BITRATES = ('original', '64k', '96k', '128k', '192k', '256k')

_PROXIES = ('proxy_bilibili', 'proxy_youtube', 'proxy_podcast')
_SECRETS = ('asr_api_key', 'bilibili_cookie')


def default_media_settings() -> dict[str, Any]:
    return {
        'enabled': True,
        'asr_protocol': 'crispasr-server',
        'asr_tier': TIER_ACCURATE,
        'asr_endpoints': dict(_DEFAULT_ENDPOINTS),
        'asr_api_key': '',
        'asr_language': 'zh',
        'asr_timeout_seconds': 900,
        #: Only relevant for LLM-style backends: their default generation cap is
        #: far below what one chunk needs (see PLAN §1.7).
        'asr_max_new_tokens': 2048,
        'chunk_seconds': 300,
        #: A very short tail chunk makes the decoder loop on garbage; merge it
        #: into the previous chunk instead (PLAN §5.3).
        'min_tail_seconds': 60,
        'max_chunk_seconds': 500,
        'concurrency': 1,
        #: Cost gate: how many new items one channel may enqueue per sync.
        'items_per_sync': 5,
        'max_duration_seconds': 14400,
        'transcribe_max_attempts': 3,
        'keep_media': True,
        'keep_media_bitrate': '128k',
        'bilibili_cc_subtitle': True,
        'bilibili_cookie': '',
        'youtube_cookies_file': '',
        'proxy_bilibili': '',
        'proxy_youtube': '',
        'proxy_podcast': '',
    }


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {'1', 'true', 'yes', 'on'}:
            return True
        if lowered in {'0', 'false', 'no', 'off', ''}:
            return False
    return default


def _as_int(value: Any, default: int, bounds: tuple[int, int]) -> int:
    try:
        number = int(value)
    except TypeError, ValueError:
        return default
    low, high = bounds
    return min(max(number, low), high)


def _as_choice(value: Any, allowed: tuple[str, ...], default: str) -> str:
    text = str(value or '').strip()
    return text if text in allowed else default


def _as_text(value: Any, default: str = '') -> str:
    if value is None:
        return default
    return str(value).strip()


def _normalize_endpoints(value: Any, *, current: dict[str, str]) -> dict[str, str]:
    """Keep only known tiers, and only http(s) URLs.

    A blank value disables that tier on purpose, so it is preserved rather than
    replaced by the default: an admin who clears the field means it.
    """
    result = dict(current)
    if not isinstance(value, dict):
        return result
    for tier in ASR_TIERS:
        if tier not in value:
            continue
        raw = str(value[tier] or '').strip().rstrip('/')
        if not raw or raw.startswith(('http://', 'https://')):
            result[tier] = raw
    return {tier: result.get(tier, '') for tier in ASR_TIERS}


def normalize_media_settings(values: Any, *, current: dict[str, Any] | None = None) -> dict[str, Any]:
    """Merge stored (or submitted) values over the defaults and coerce types.

    Unknown keys are dropped, which is what keeps a stale key from an older
    deployment harmless.
    """
    base = default_media_settings()
    if current:
        base.update(current)
    stored = values if isinstance(values, dict) else {}
    for key, value in stored.items():
        if key in base:
            base[key] = value

    base['enabled'] = _as_bool(base['enabled'], True)
    base['asr_protocol'] = _as_choice(base['asr_protocol'], ASR_PROTOCOLS, 'crispasr-server')
    base['asr_tier'] = _as_choice(base['asr_tier'], ASR_TIERS, TIER_ACCURATE)
    endpoints = _normalize_endpoints(base['asr_endpoints'], current=dict(_DEFAULT_ENDPOINTS))
    if 'asr_endpoints' in stored:
        endpoints = _normalize_endpoints(stored['asr_endpoints'], current=endpoints)
    base['asr_endpoints'] = endpoints
    base['asr_language'] = _as_text(base['asr_language'], 'zh') or 'zh'
    base['asr_timeout_seconds'] = _as_int(base['asr_timeout_seconds'], 900, _TIMEOUT_SECONDS_RANGE)
    base['asr_max_new_tokens'] = _as_int(base['asr_max_new_tokens'], 2048, (128, 32768))
    base['chunk_seconds'] = _as_int(base['chunk_seconds'], 300, _CHUNK_SECONDS_RANGE)
    base['min_tail_seconds'] = _as_int(base['min_tail_seconds'], 60, _TAIL_SECONDS_RANGE)
    base['max_chunk_seconds'] = _as_int(base['max_chunk_seconds'], 500, _MAX_CHUNK_SECONDS_RANGE)
    base['concurrency'] = _as_int(base['concurrency'], 1, _CONCURRENCY_RANGE)
    base['items_per_sync'] = _as_int(base['items_per_sync'], 5, _ITEMS_PER_SYNC_RANGE)
    base['max_duration_seconds'] = _as_int(base['max_duration_seconds'], 14400, _MAX_DURATION_SECONDS_RANGE)
    base['transcribe_max_attempts'] = _as_int(base['transcribe_max_attempts'], 3, _ATTEMPTS_RANGE)
    base['keep_media'] = _as_bool(base['keep_media'], True)
    base['keep_media_bitrate'] = _as_choice(base['keep_media_bitrate'], BITRATES, '128k')
    base['bilibili_cc_subtitle'] = _as_bool(base['bilibili_cc_subtitle'], True)
    for key in (*_SECRETS, *_PROXIES, 'youtube_cookies_file'):
        base[key] = _as_text(base[key])
    # A short tail is joined onto the previous chunk rather than submitted on
    # its own, so the ceiling has to leave room for that join (media/audio.py).
    base['min_tail_seconds'] = min(base['min_tail_seconds'], base['chunk_seconds'])
    base['max_chunk_seconds'] = max(base['max_chunk_seconds'], base['chunk_seconds'] + base['min_tail_seconds'])
    return base


async def get_media_settings(storage: PostgresStorage) -> dict[str, Any]:
    stored = await load_meta_json(storage, MEDIA_SETTINGS_KEY, {})
    return normalize_media_settings(stored)


async def set_media_settings(storage: PostgresStorage, updates: dict[str, Any]) -> dict[str, Any]:
    """Merge ``updates`` into the stored settings and return the result."""
    current = await get_media_settings(storage)
    settings = normalize_media_settings(updates, current=current)
    async with storage.transaction():
        await save_meta_json(storage, MEDIA_SETTINGS_KEY, settings)
    return settings


def asr_endpoint(settings: dict[str, Any], tier: str) -> str:
    """Return the configured base URL for ``tier``, or raise when it is unset."""
    endpoints = settings.get('asr_endpoints') or {}
    endpoint = str(endpoints.get(tier) or '').strip()
    if not endpoint:
        raise ValueError(f'ASR tier {tier!r} has no endpoint configured')
    return endpoint


__all__ = [
    'ASR_PROTOCOLS',
    'ASR_TIERS',
    'BITRATES',
    'MEDIA_SETTINGS_KEY',
    'TIER_ACCURATE',
    'TIER_FAST',
    'asr_endpoint',
    'default_media_settings',
    'get_media_settings',
    'normalize_media_settings',
    'set_media_settings',
]
