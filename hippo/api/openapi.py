"""The OpenAPI document served at ``/openapi.json`` and rendered at ``/docs``.

Three things are decided here instead of next to each route:

* **Branding and version.** The title is Hippo API and the version is the one in
  ``hippo/__init__.py``, which reads it back from the installed distribution that
  ``pyproject.toml`` owns.
* **Security.** Every operation's ``security`` is read off the route's own
  dependency graph. A hand-written annotation can drift from the check that
  actually runs, and a document that promises a cookie where the code wants an
  admin is worse than no document. ``hippo.api.deps`` remains the only place
  that decides access; this module only reports it.
* **The error envelope.** FastAPI documents its own validation body by default;
  the API answers ``{error, code}`` for every failure (see ``hippo.api.errors``),
  so that body is registered as ``ApiError`` and referenced where a failure is
  expected.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi.routing import APIRoute

from . import deps
from .schemas.common import ErrorBody

APP_TITLE = 'Hippo API'
APP_SUMMARY = 'Self-hosted personal information and knowledge platform.'

APP_DESCRIPTION = """\
Hippo subscribes to content from several sources, keeps a copy of what it
fetched, and serves it back to its owner.

* **Sources** — WeChat public accounts (`weixin`), Bilibili and YouTube
  channels, and podcast or RSS feeds (`podcast`). A subscription is created by
  pasting one URL: `GET /api/source/preview` says what it resolves to and
  `POST /api/source/channel` subscribes. `GET /api/source` lists the sources the
  caller actually follows.
* **What it stores** — the item and its body, images, archived audio/video, and
  the transcript derived from it. `GET /api/article/{article_id}` returns the
  body as content blocks; `GET /api/media/{article_pk}/{kind}` streams the
  archived media and `GET /api/article/{article_id}/transcript` exports the
  words as markdown, text or SRT.
* **Knowledge bases** — private documents (`/api/knowledge-base`) that are
  parsed into chunks and searched in full text.
* **Access** — a browser session cookie, or an API key for clients that cannot
  hold one (`/api/apikey`, `GET /api/whoami`). An API key may read but may not
  reach administration or credential endpoints, and it may only write with the
  `write` scope.
"""

#: Tag per top-level path segment. One table, so a new router cannot be
#: documented under a tag nobody defined; ``knowledge`` also sets it on its own
#: router, and the two agree.
_TAG_BY_SEGMENT: dict[str, str] = {
    'account': 'account',
    'admin': 'admin',
    'apikey': 'api-key',
    'article': 'article',
    'auth': 'auth',
    'chat': 'chat',
    'document': 'knowledge',
    'feed': 'feed',
    'group': 'group',
    'image': 'article',
    'knowledge-base': 'knowledge',
    'llm': 'llm',
    'login': 'daemon',
    'media': 'article',
    'report': 'report',
    'settings': 'settings',
    'source': 'source',
    'whoami': 'api-key',
}

OPENAPI_TAGS: list[dict[str, str]] = [
    {'name': 'auth', 'description': 'Sign-in, sessions, registration and password or e-mail changes.'},
    {'name': 'api-key', 'description': 'API keys for clients that cannot hold a cookie, and `GET /api/whoami`.'},
    {'name': 'account', 'description': 'Subscribed channels: list, search, subscribe and per-user settings.'},
    {'name': 'group', 'description': 'Groups a user files subscriptions into.'},
    {
        'name': 'article',
        'description': 'Articles, highlights, images, transcripts and archived media. '
        'Covers every source: the item shape is the same for a WeChat post and a video.',
    },
    {'name': 'source', 'description': 'Pasting a URL to subscribe, and per-user source credentials.'},
    {'name': 'daemon', 'description': 'The WeChat account the crawler runs as. Not Hippo authentication.'},
    {'name': 'chat', 'description': 'Reading assistant conversations. Answers stream as server-sent events.'},
    {'name': 'llm', 'description': 'LLM providers used by the reading assistant. Admin only.'},
    {'name': 'report', 'description': 'Daily report settings, preview and manual delivery.'},
    {'name': 'knowledge', 'description': 'Private knowledge bases, their documents, parsed content and search.'},
    {'name': 'admin', 'description': 'Instance administration: users, site, media, audit and logs.'},
    {'name': 'settings', 'description': 'Sync settings, reading preferences, tasks and status.'},
    {'name': 'feed', 'description': 'Mixed feed for readers and RSS clients.'},
]

#: Cookie session, and the two shapes an API key may arrive in.
SECURITY_SCHEMES: dict[str, dict[str, Any]] = {
    'hippo_session': {
        'type': 'apiKey',
        'in': 'cookie',
        'name': deps.SESSION_COOKIE,
        'description': 'Session cookie set by `POST /api/auth/login`.',
    },
    'api_key_bearer': {
        'type': 'http',
        'scheme': 'bearer',
        'description': 'API key as `Authorization: Bearer hippo_…`.',
    },
    'api_key_header': {
        'type': 'apiKey',
        'in': 'header',
        'name': 'X-API-Key',
        'description': 'The same API key in its header form.',
    },
    'feed_token': {
        'type': 'apiKey',
        'in': 'query',
        'name': 'token',
        'description': 'Long-lived session token, for feed readers that cannot send a cookie.',
    },
}

_SESSION_SECURITY: dict[str, list[str]] = {'hippo_session': []}
_API_KEY_SECURITY: tuple[dict[str, list[str]], ...] = ({'api_key_bearer': []}, {'api_key_header': []})

#: Routes that accept something extra on top of the standard credentials.
_EXTRA_SECURITY_BY_PATH: dict[str, tuple[dict[str, list[str]], ...]] = {
    '/api/feed/mixed': ({'feed_token': []},),
}

_HTTP_METHODS = ('get', 'post', 'put', 'patch', 'delete')

_ERROR_BODY_REF = {'$ref': '#/components/schemas/ErrorBody'}


def install_openapi(app: FastAPI) -> None:
    """Make ``/openapi.json`` the branded, security-aware document."""
    default_openapi = app.openapi

    def custom_openapi() -> dict[str, Any]:
        if app.openapi_schema:
            return app.openapi_schema
        schema = default_openapi()
        _register_error_body(schema)
        _apply_tags(schema)
        _apply_security(schema, app)
        _register_security_schemes(schema)
        _apply_error_responses(schema)
        app.openapi_schema = schema
        return schema

    app.openapi = custom_openapi


def _register_error_body(schema: dict[str, Any]) -> None:
    components = schema.setdefault('components', {})
    components.setdefault('schemas', {})['ErrorBody'] = ErrorBody.model_json_schema()


def _apply_tags(schema: dict[str, Any]) -> None:
    for path, operations in schema.get('paths', {}).items():
        tag = _tag_for_path(path)
        if tag is None:
            continue
        for method in _HTTP_METHODS:
            if method in operations:
                operations[method]['tags'] = [tag]


def _tag_for_path(path: str) -> str | None:
    segments = [segment for segment in path.split('/') if segment]
    if not segments or segments[0] != 'api':
        return None
    return _TAG_BY_SEGMENT.get(segments[1]) if len(segments) > 1 else None


def _dependency_names(dependant: Any) -> set[str]:
    """Names of every callable in a route's dependency tree."""
    names: set[str] = set()
    for sub in dependant.dependencies:
        name = getattr(sub.call, '__name__', '')
        if name:
            names.add(name)
        names |= _dependency_names(sub)
    return names


def _required_security(path: str, names: set[str]) -> list[dict[str, list[str]]] | None:
    """The credential alternatives this route really accepts, or ``None``.

    ``None`` means the route is public and no ``security`` is emitted, which is
    what an operation without a credential must look like.
    """
    if 'require_admin' in names:
        # Administration refuses API keys outright (deps.API_KEY_FORBIDDEN_PREFIXES).
        return [_SESSION_SECURITY]
    if 'current_user' in names:
        if any(path.startswith(prefix) for prefix in deps.API_KEY_FORBIDDEN_PREFIXES):
            return [_SESSION_SECURITY]
        return [_SESSION_SECURITY, *_API_KEY_SECURITY]
    if 'current_user_optional' in names:
        return [_SESSION_SECURITY, *_API_KEY_SECURITY, *_EXTRA_SECURITY_BY_PATH.get(path, ())]
    return None


def _apply_security(schema: dict[str, Any], app: FastAPI) -> None:
    paths = schema.get('paths', {})
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        required = _required_security(route.path, _dependency_names(route.dependant))
        if required is None:
            continue
        operations = paths.get(route.path)
        if not operations:
            continue
        for method in route.methods:
            operation = operations.get(method.lower())
            if operation is not None:
                operation['security'] = required


def _register_security_schemes(schema: dict[str, Any]) -> None:
    """Declare the credentials the operations above actually reference."""
    used = {
        name
        for operations in schema.get('paths', {}).values()
        for method in _HTTP_METHODS
        for requirement in (operations.get(method) or {}).get('security') or []
        for name in requirement
    }
    if not used:
        return
    components = schema.setdefault('components', {})
    components['securitySchemes'] = {name: definition for name, definition in SECURITY_SCHEMES.items() if name in used}


def _error_response(description: str) -> dict[str, Any]:
    return {
        'description': description,
        'content': {'application/json': {'schema': dict(_ERROR_BODY_REF)}},
    }


def _apply_error_responses(schema: dict[str, Any]) -> None:
    """Point the failure statuses at the body the API actually returns.

    FastAPI's generated 422 describes its own validation body; a request that
    fails validation is answered by ``install_exception_handlers`` in the same
    ``{error, code}`` envelope as everything else, so it is replaced here.
    """
    for operations in schema.get('paths', {}).values():
        for method in _HTTP_METHODS:
            operation = operations.get(method)
            if operation is None:
                continue
            responses = operation.setdefault('responses', {})
            if '422' in responses:
                responses['422'] = _error_response('Request validation failed.')
            if operation.get('security'):
                responses.setdefault('401', _error_response('No usable session cookie or API key.'))
                responses.setdefault('403', _error_response('Signed in, but not allowed.'))
            responses.setdefault('500', _error_response('Unexpected server error.'))
