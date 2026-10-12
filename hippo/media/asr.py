"""Calling the ASR endpoint.

CrispASR's server speaks the OpenAI audio-transcription protocol, so one client
covers it and anything else compatible: a protocol switch in the settings would
just be a second name for the same request.

The endpoint is not OpenAI, which is why this talks to it directly instead of
through the SDK: we need a multipart upload and two response fields, and typed
response models for a foreign server buy nothing. Errors are translated into the
same kind of Chinese sentence the LLM client produces, so a misconfigured backend
reads as "认证失败" rather than a stack trace.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from ..observability.otel import traced

TRANSCRIPTIONS_PATH = '/audio/transcriptions'
HEALTH_PATH = '/health'

#: Content type of the chunks we upload; `media/audio.py` produces mp3.
_AUDIO_CONTENT_TYPE = 'audio/mpeg'


class AsrError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class Segment:
    """One timed piece of the transcript, relative to the uploaded chunk."""

    start_seconds: float
    end_seconds: float
    text: str


@dataclass(frozen=True, slots=True)
class Transcript:
    text: str
    segments: list[Segment]
    language: str | None = None

    @property
    def duration_seconds(self) -> float:
        if not self.segments:
            return 0.0
        return max(segment.end_seconds for segment in self.segments)


def health_url(endpoint: str) -> str:
    """``…:8317/v1`` → ``…:8317/health`` (what a CrispASR server exposes)."""
    base = endpoint.strip().rstrip('/')
    if base.endswith('/v1'):
        base = base[: -len('/v1')]
    return f'{base}{HEALTH_PATH}'


def _detail(response: httpx.Response) -> str:
    try:
        return str(response.text)[:300]
    except Exception:  # pragma: no cover - only when the body cannot be read
        return ''


def _http_error(status: int, body: str) -> AsrError:
    if status in (401, 403):
        return AsrError('转写服务认证失败，请检查 API Key')
    if status == 404:
        return AsrError('转写服务找不到该接口，请检查端点地址')
    if status == 429:
        return AsrError('转写服务限流，请稍后再试')
    if status >= 500:
        return AsrError(f'转写服务内部错误（HTTP {status}）：{body}')
    return AsrError(f'转写服务拒绝了请求（HTTP {status}）：{body}')


def _transport_error(exc: httpx.HTTPError) -> AsrError:
    if isinstance(exc, httpx.TimeoutException):
        return AsrError('转写服务超时')
    if isinstance(exc, httpx.ConnectError):
        return AsrError('无法连接转写服务，请检查端点地址与网络')
    return AsrError(f'转写请求失败：{exc}')


def _parse_segments(payload: dict[str, Any]) -> list[Segment]:
    segments: list[Segment] = []
    for raw in payload.get('segments') or []:
        if not isinstance(raw, dict):
            continue
        text = str(raw.get('text') or '').strip()
        if not text:
            continue
        try:
            start = float(raw.get('start') or 0.0)
            end = float(raw.get('end') or start)
        except TypeError, ValueError:
            continue
        segments.append(Segment(start_seconds=start, end_seconds=end, text=text))
    return segments


class AsrClient:
    """One tier's endpoint, reused across chunks so the connection is pooled."""

    def __init__(
        self,
        *,
        endpoint: str,
        api_key: str = '',
        language: str = 'zh',
        timeout: float = 900.0,
        max_new_tokens: int = 2048,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.endpoint = endpoint.strip().rstrip('/')
        if not self.endpoint:
            raise ValueError('ASR endpoint is empty')
        self._api_key = api_key
        self._language = language
        self._max_new_tokens = max_new_tokens
        self._client = client or httpx.AsyncClient(timeout=timeout)
        self._owns_client = client is None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def _headers(self) -> dict[str, str]:
        return {'Authorization': f'Bearer {self._api_key}'} if self._api_key else {}

    async def health(self) -> dict[str, Any]:
        """Ask the server what it loaded. Raises when it is not answering."""
        try:
            response = await self._client.get(health_url(self.endpoint))
        except httpx.HTTPError as exc:
            raise _transport_error(exc) from exc
        if response.status_code != 200:
            raise _http_error(response.status_code, _detail(response))
        try:
            payload = response.json()
        except ValueError as exc:
            raise AsrError('转写服务的健康检查没有返回 JSON') from exc
        if not isinstance(payload, dict):
            raise AsrError('转写服务的健康检查返回了意外的结构')
        return payload

    @traced('asr.transcribe')
    async def transcribe(self, audio: Path, *, language: str | None = None) -> Transcript:
        """Upload one chunk and return its transcript.

        ``max_new_tokens`` is sent per request even though the server was started
        with a cap: a server whose default is lower would silently truncate the
        chunk (the LLM-style backends default to 512).
        """
        try:
            payload = audio.read_bytes()
        except OSError as exc:
            raise AsrError(f'读取音频切片失败：{exc}') from exc
        data = {
            'response_format': 'verbose_json',
            'language': language or self._language,
            'max_new_tokens': str(self._max_new_tokens),
        }
        try:
            response = await self._client.post(
                f'{self.endpoint}{TRANSCRIPTIONS_PATH}',
                headers=self._headers(),
                data=data,
                files={'file': (audio.name, payload, _AUDIO_CONTENT_TYPE)},
            )
        except httpx.HTTPError as exc:
            raise _transport_error(exc) from exc
        if response.status_code != 200:
            raise _http_error(response.status_code, _detail(response))
        try:
            body = response.json()
        except (ValueError, json.JSONDecodeError) as exc:
            raise AsrError('转写服务没有返回 JSON') from exc
        if not isinstance(body, dict):
            raise AsrError('转写服务返回了意外的结构')
        text = body.get('text')
        if not isinstance(text, str):
            raise AsrError('转写服务响应缺少 text 字段')
        language_out = body.get('language')
        return Transcript(
            text=text.strip(),
            segments=_parse_segments(body),
            language=str(language_out) if language_out else None,
        )


def client_for(settings: dict[str, Any], tier: str) -> AsrClient:
    """Build the client for a tier out of the media settings.

    Raises ``ValueError`` when that tier has no endpoint, which is a settings
    problem the caller reports as-is rather than a transport failure.
    """
    from ..media_settings import asr_endpoint

    return AsrClient(
        endpoint=asr_endpoint(settings, tier),
        api_key=str(settings.get('asr_api_key') or ''),
        language=str(settings.get('asr_language') or 'zh'),
        timeout=float(settings.get('asr_timeout_seconds') or 900),
        max_new_tokens=int(settings.get('asr_max_new_tokens') or 2048),
    )


__all__ = [
    'HEALTH_PATH',
    'TRANSCRIPTIONS_PATH',
    'AsrClient',
    'AsrError',
    'Segment',
    'Transcript',
    'client_for',
    'health_url',
]
