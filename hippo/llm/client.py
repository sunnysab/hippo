"""LLM client.

One place owns the provider lookup, the concurrency gate, timeouts and the
translation of SDK exceptions into Chinese messages a reader can act on. A
misconfigured provider must produce "未配置可用的 LLM provider", not a 500 with
a stack trace.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import openai
from openai import AsyncOpenAI

from ..exceptions import ApiError
from ..logger import get_logger
from ..observability.otel import span
from ..storage import PostgresStorage

logger = get_logger(__name__)

#: Requests in flight at once. A local vLLM serves one conversation quickly;
#: more parallelism just queues inside the model server.
MAX_CONCURRENT = 2

#: Per-request budget. Long enough for a full article summary, short enough that
#: a hung upstream does not hold a connection for minutes.
REQUEST_TIMEOUT = 120.0

_gate = asyncio.Semaphore(MAX_CONCURRENT)


class LlmUnavailableError(ApiError):
    """The provider is missing or misconfigured."""

    def __init__(self, message: str) -> None:
        super().__init__(message, status=503)


def _classify(exc: Exception) -> tuple[str, int]:
    """Map an SDK exception to (Chinese message, HTTP status)."""
    if isinstance(exc, openai.AuthenticationError):
        return 'LLM 认证失败，请检查 API Key', 502
    if isinstance(exc, openai.PermissionDeniedError):
        return 'LLM 拒绝了该请求（权限不足）', 502
    if isinstance(exc, openai.NotFoundError):
        return 'LLM 模型或接口不存在，请检查 base_url 与模型名', 502
    if isinstance(exc, openai.RateLimitError):
        return 'LLM 限流，请稍后再试', 429
    if isinstance(exc, openai.APITimeoutError):
        return 'LLM 响应超时', 504
    if isinstance(exc, openai.APIConnectionError):
        return '无法连接 LLM 服务，请检查网络与 base_url', 502
    if isinstance(exc, openai.BadRequestError):
        return 'LLM 拒绝了该请求（参数不合法）', 502
    if isinstance(exc, openai.APIStatusError):
        return f'LLM 返回错误状态 {exc.status_code}', 502
    return 'LLM 调用失败', 502


@asynccontextmanager
async def _client(provider: dict[str, Any]) -> AsyncIterator[AsyncOpenAI]:
    with span('llm.request', model=provider.get('model'), provider=provider.get('name')):
        client = AsyncOpenAI(
            base_url=provider['base_url'],
            api_key=provider['api_key'],
            timeout=REQUEST_TIMEOUT,
            max_retries=0,
        )
        try:
            yield client
        finally:
            await client.close()


async def resolve_provider(
    storage: PostgresStorage,
    provider_id: int | None = None,
) -> dict[str, Any]:
    """Return the provider for a session, falling back to the default.

    The explicit id wins so a session keeps using the provider it started with;
    deleting that provider falls back rather than failing the conversation.
    """
    if provider_id is not None:
        provider = await storage.llm.get_full(provider_id)
        if provider and provider['enabled']:
            return provider
    provider = await storage.llm.get_default()
    if not provider:
        raise LlmUnavailableError('未配置可用的 LLM provider，请先在管理面板添加')
    return provider


async def stream_chat(
    provider: dict[str, Any],
    messages: list[dict[str, str]],
    *,
    temperature: float = 0.3,
    max_tokens: int | None = None,
) -> AsyncIterator[str]:
    """Yield assistant text deltas from a streaming completion.

    The semaphore wraps the whole stream, not just the call: the request is only
    finished once the last delta arrives.
    """
    async with _gate:
        try:
            async with _client(provider) as client:
                stream = await client.chat.completions.create(
                    model=provider['model'],
                    messages=messages,  # type: ignore[arg-type]
                    temperature=temperature,
                    max_tokens=max_tokens,
                    stream=True,
                )
                async for chunk in stream:
                    if not chunk.choices:
                        continue
                    delta = chunk.choices[0].delta
                    text = getattr(delta, 'content', None)
                    if text:
                        yield text
        except ApiError:
            raise
        except Exception as exc:
            message, status = _classify(exc)
            logger.warning(
                'LLM stream failed (model=%s, provider=%s): %s',
                provider.get('model'),
                provider.get('name'),
                exc,
            )
            raise ApiError(message, status=status) from exc


class DecisionUnavailableError(ApiError):
    """No provider speaks the decision protocol."""

    def __init__(self, message: str) -> None:
        super().__init__(message, status=503)


async def resolve_decision_provider(storage: PostgresStorage) -> dict[str, Any]:
    """The enabled provider that speaks /v1/systemone.

    A decision model is not interchangeable with a chat model: it answers with
    probabilities and cannot hold a conversation. Falling back to a chat provider
    here would produce values the caller cannot use, so this refuses instead.
    """
    for provider in await storage.llm.list_all():
        if provider['enabled'] and provider.get('kind') == 'decision':
            full = await storage.llm.get_full(provider['id'])
            if full:
                return full
    raise DecisionUnavailableError('未配置 decision 类型的 provider（如 clef-flash）')


async def decide(
    provider: dict[str, Any],
    *,
    state: str,
    questions: dict[str, Any],
) -> dict[str, Any]:
    """One forward pass over ``state``, answering every question at once.

    This is Ollama's ``/v1/systemone`` (the System One API). It is not the OpenAI
    chat shape: the model scores each option jointly and returns probabilities,
    producing zero output tokens. Kept apart from ``complete`` on purpose --
    routing these through the OpenAI SDK would mean pretending a scoring model is
    a text generator.
    """
    url = provider['base_url'].rstrip('/') + '/v1/systemone'
    payload = {'model': provider['model'], 'state': state, 'questions': questions}
    headers = {'Content-Type': 'application/json'}
    if provider.get('api_key'):
        headers['Authorization'] = f'Bearer {provider["api_key"]}'
    async with _gate:
        with span('llm.decide', model=provider.get('model'), provider=provider.get('name')):
            try:
                async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
                    response = await client.post(url, json=payload, headers=headers)
                    response.raise_for_status()
                    body = response.json()
            except httpx.HTTPStatusError as exc:
                logger.warning('LLM decision failed: %s %s', exc.response.status_code, exc.response.text[:200])
                raise ApiError(f'LLM 返回错误状态 {exc.response.status_code}', status=502) from exc
            except httpx.HTTPError as exc:
                logger.warning('LLM decision unreachable: %s', exc)
                raise ApiError('无法连接 LLM 服务，请检查网络与 base_url', status=502) from exc
    answers = body.get('answers')
    if not isinstance(answers, dict):
        raise ApiError('LLM 返回的 decision 结果无法解析', status=502)
    return answers


async def complete(
    provider: dict[str, Any],
    messages: list[dict[str, str]],
    *,
    temperature: float = 0.3,
    max_tokens: int | None = None,
) -> str:
    """Non-streaming completion, for callers that want the whole answer at once."""
    async with _gate:
        try:
            async with _client(provider) as client:
                response = await client.chat.completions.create(
                    model=provider['model'],
                    messages=messages,  # type: ignore[arg-type]
                    temperature=temperature,
                    max_tokens=max_tokens,
                )
        except ApiError:
            raise
        except Exception as exc:
            message, status = _classify(exc)
            logger.warning('LLM completion failed: %s', exc)
            raise ApiError(message, status=status) from exc
    if not response.choices:
        return ''
    return response.choices[0].message.content or ''
