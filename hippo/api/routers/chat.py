"""Chat sessions, streaming answers and article reading actions.

Streaming contract: a partial answer is never stored. The assistant turn is
written only after the upstream stream ends cleanly, so a client that
disconnects mid-answer leaves the conversation exactly as it was and can simply
ask again.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Body, Depends, Request, status
from fastapi.responses import StreamingResponse

from ...article_queries import _get_article
from ...exceptions import ApiError
from ...llm.client import resolve_provider, stream_chat
from ...llm.prompt import PRESETS, preset_marker, strip_preset_marker, system_message
from ...llm.prompt import build_context as build_article_context
from ...logger import get_logger
from ...models import User
from ...storage import PostgresStorage, open_storage
from ..deps import current_user, get_storage

logger = get_logger(__name__)
router = APIRouter(dependencies=[Depends(current_user)])

#: Turns replayed to the model. Enough for a long reading session without
#: letting an old conversation crowd out the article.
HISTORY_LIMIT = 20

#: Answer length ceiling.
MAX_ANSWER_TOKENS = 2000


def _session_id(raw: str) -> int:
    try:
        return int(raw)
    except (TypeError, ValueError) as exc:
        raise ApiError('Invalid session id', status=400) from exc


def _sse(payload: dict[str, Any]) -> str:
    return f'data: {json.dumps(payload, ensure_ascii=False)}\n\n'


@router.get('/chat/session')
async def list_sessions(
    user: User = Depends(current_user),
    storage: PostgresStorage = Depends(get_storage),
) -> dict[str, Any]:
    """Every conversation this user has, newest first."""
    return {'items': await storage.chat.list_sessions(user.id)}


@router.post('/chat/session', status_code=status.HTTP_201_CREATED)
async def create_session(
    body: dict[str, Any] = Body(default={}),
    user: User = Depends(current_user),
    storage: PostgresStorage = Depends(get_storage),
) -> dict[str, Any]:
    """Open a conversation, reusing the article's existing one when there is one."""
    raw_article_id = body.get('article_id')
    article_pk: int | None = None
    title = str(body.get('title') or '').strip()

    if raw_article_id is not None:
        try:
            article_pk = int(raw_article_id)
        except (TypeError, ValueError) as exc:
            raise ApiError('Invalid article_id', status=400) from exc
        # 404 unless the article is visible to this user.
        await _get_article(storage, user.id, article_pk)
        existing = await storage.chat.get_session_for_article(user.id, article_pk)
        if existing:
            return existing

    if not title:
        title = '文章解读' if article_pk is not None else '新对话'

    async with storage.transaction():
        return await storage.chat.create_session(user_id=user.id, title=title, article_pk=article_pk)


@router.get('/chat/session/{session_id}')
async def get_session(
    session_id: str,
    user: User = Depends(current_user),
    storage: PostgresStorage = Depends(get_storage),
) -> dict[str, Any]:
    """A session with its full message history."""
    session = await storage.chat.get_session(_session_id(session_id), user.id)
    if session is None:
        raise ApiError('会话不存在', status=404)
    messages = await storage.chat.list_messages(session['id'])
    # Preset answers carry an internal marker; render them clean.
    for message in messages:
        message['content'] = strip_preset_marker(message['content'])
        message['preset'] = _preset_of(message)
    return {'session': session, 'messages': messages}


@router.post('/chat/session/{session_id}/rename', status_code=status.HTTP_204_NO_CONTENT)
async def rename_session(
    session_id: str,
    body: dict[str, Any] = Body(default={}),
    user: User = Depends(current_user),
    storage: PostgresStorage = Depends(get_storage),
) -> None:
    title = str(body.get('title') or '').strip()
    if not title:
        raise ApiError('标题不能为空', status=400)
    async with storage.transaction():
        renamed = await storage.chat.rename_session(_session_id(session_id), user.id, title)
    if not renamed:
        raise ApiError('会话不存在', status=404)


@router.delete('/chat/session/{session_id}', status_code=status.HTTP_204_NO_CONTENT)
async def delete_session(
    session_id: str,
    user: User = Depends(current_user),
    storage: PostgresStorage = Depends(get_storage),
) -> None:
    async with storage.transaction():
        deleted = await storage.chat.delete_session(_session_id(session_id), user.id)
    if not deleted:
        raise ApiError('会话不存在', status=404)


@router.get('/chat/session/{session_id}/preset/{preset}')
async def read_preset(
    session_id: str,
    preset: str,
    user: User = Depends(current_user),
    storage: PostgresStorage = Depends(get_storage),
) -> dict[str, Any]:
    """Return a cached preset answer without calling the model.

    Summaries and key points depend only on the article, so a previous answer is
    still correct; reopening the sidebar must not cost an LLM request.
    """
    _require_preset(preset)
    session = await storage.chat.get_session(_session_id(session_id), user.id)
    if session is None:
        raise ApiError('会话不存在', status=404)
    cached = await storage.chat.find_preset(session['id'], preset)
    if cached is None:
        return {'available': False}
    return {
        'available': True,
        'content': strip_preset_marker(cached['content']),
        'message_id': cached['id'],
        'created_at': cached['created_at'],
    }


@router.post('/chat/session/{session_id}/message')
async def send_message(
    session_id: str,
    request: Request,
    body: dict[str, Any] = Body(default={}),
    user: User = Depends(current_user),
    storage: PostgresStorage = Depends(get_storage),
) -> StreamingResponse:
    """Send a turn and stream the answer back as server-sent events.

    ``preset`` (summary, points) sends a canned instruction instead of user
    text; ``content`` sends a free-form question.
    """
    target_id = _session_id(session_id)
    session = await storage.chat.get_session(target_id, user.id)
    if session is None:
        raise ApiError('会话不存在', status=404)

    preset = str(body.get('preset') or '').strip()
    content = str(body.get('content') or '').strip()
    if not preset and not content:
        raise ApiError('消息内容不能为空', status=400)
    if preset:
        _require_preset(preset)

    prompt = PRESETS[preset] if preset else content

    # A preset answer goes straight back to the client when it already exists.
    if preset:
        cached = await storage.chat.find_preset(target_id, preset)
        if cached is not None:
            return StreamingResponse(
                _replay(strip_preset_marker(cached['content']), cached['id']),
                media_type='text/event-stream',
            )

    context, truncated = await _article_context(storage, user.id, session)

    provider = await resolve_provider(storage, session.get('provider_id'))
    if session.get('provider_id') != provider['id']:
        async with storage.transaction():
            await storage.chat.set_provider(target_id, user.id, provider['id'])

    history = await storage.chat.list_messages(target_id, limit=HISTORY_LIMIT)
    messages = [system_message(context)]
    for message in history:
        messages.append({'role': message['role'], 'content': strip_preset_marker(message['content'])})
    messages.append({'role': 'user', 'content': prompt})

    # For a preset there is no user-authored turn worth showing in the
    # transcript, so only the assistant answer is stored.
    if content:
        async with storage.transaction():
            await storage.chat.append_message(target_id, 'user', content)

    return StreamingResponse(
        _stream(target_id, provider, messages, preset, truncated),
        media_type='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'},
    )


async def _article_context(
    storage: PostgresStorage,
    user_id: int,
    session: dict[str, Any],
) -> tuple[str, bool]:
    """Build the system context for a session.

    Free-form chat has no article, so it gets a short self-description instead.
    """
    article_id = session.get('article_id')
    if article_id is None:
        return '当前没有关联文章，这是一次自由对话。', False
    payload = await _get_article(storage, user_id, article_id)
    return build_article_context(payload.get('article') or {}, payload.get('content'))


async def _replay(content: str, message_id: int) -> AsyncIterator[str]:
    yield _sse({'delta': content})
    yield _sse({'done': True, 'message_id': message_id, 'cached': True})


async def _stream(
    session_id: int,
    provider: dict[str, Any],
    messages: list[dict[str, str]],
    preset: str,
    truncated: bool,
) -> AsyncIterator[str]:
    """Forward deltas, then persist the answer — but only if it completed."""
    if truncated:
        yield _sse({'truncated': True})

    collected: list[str] = []
    completed = False
    try:
        async for delta in stream_chat(provider, messages, max_tokens=MAX_ANSWER_TOKENS):
            collected.append(delta)
            yield _sse({'delta': delta})
        completed = True
    except ApiError as exc:
        # The status is already lost once the stream started, so the error
        # travels as an event the client can render.
        yield _sse({'error': str(exc)})
    except Exception as exc:  # pragma: no cover - defensive
        logger.exception('Chat stream failed for session %s: %s', session_id, exc)
        yield _sse({'error': '生成失败，请重试'})

    if not completed or not collected:
        # Nothing is stored: a half answer would poison every later turn, and
        # the client is expected to offer a retry.
        return

    answer = ''.join(collected)
    if preset:
        answer = f'{preset_marker(preset)}\n{answer}'
    try:
        async with open_storage() as storage, storage.transaction():
            message = await storage.chat.append_message(session_id, 'assistant', answer)
        yield _sse({'done': True, 'message_id': message['id']})
    except Exception as exc:  # pragma: no cover - defensive
        logger.exception('Failed to store chat answer for session %s: %s', session_id, exc)
        yield _sse({'error': '回答已生成但保存失败，请重试'})


def _require_preset(preset: str) -> None:
    if preset not in PRESETS:
        raise ApiError(f'未知的预设动作: {preset}', status=400)


def _preset_of(message: dict[str, Any]) -> str | None:
    content = message.get('content') or ''
    if not content.startswith('[preset:'):
        return None
    end = content.find(']')
    return content[7:end] if end != -1 else None
