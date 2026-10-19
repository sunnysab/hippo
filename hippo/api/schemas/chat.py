"""Reading-assistant conversation schemas: sessions, messages and preset answers."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

#: Preset actions the sidebar can ask for. They are answered from the article,
#: which is why a stored answer can be replayed without calling the model.
ChatPreset = Literal['summary', 'points']

# -- requests ----------------------------------------------------------------


class SessionCreateRequest(BaseModel):
    """``POST /api/chat/session``."""

    article_id: int | None = Field(default=None, description='Article to read with; omitted for free-form chat.')
    title: str | None = Field(default=None, description='Session title; a default is chosen when omitted.')


class SessionRenameRequest(BaseModel):
    """``POST /api/chat/session/{session_id}/rename``."""

    title: str = Field(description='New title.')


class MessageRequest(BaseModel):
    """``POST /api/chat/session/{session_id}/message``.

    Send ``content`` for a free-form question or ``preset`` for a canned
    action. The answer is streamed back as server-sent events, not JSON.
    """

    content: str | None = Field(default=None, description='Free-form question.')
    preset: ChatPreset | None = Field(default=None, description='Ask for a preset answer instead of a question.')


# -- responses ---------------------------------------------------------------


class ChatSessionOut(BaseModel):
    """One conversation."""

    id: int
    article_id: int | None = Field(default=None, description='Article it reads with, when there is one.')
    provider_id: int | None = None
    title: str
    created_at: str | None = None
    updated_at: str | None = None


class ChatMessageOut(BaseModel):
    id: int
    session_id: int
    role: str = Field(description='user / assistant / system.')
    content: str = Field(description='Preset markers are stripped; the text is what the reader saw.')
    preset: ChatPreset | None = Field(default=None, description='Set on an answer that came from a preset.')
    created_at: str | None = None


class ChatSessionListOut(BaseModel):
    items: list[ChatSessionOut]


class ChatSessionDetailOut(BaseModel):
    session: ChatSessionOut
    messages: list[ChatMessageOut]


class PresetAnswerOut(BaseModel):
    """A cached preset answer, or a plain "nothing stored yet"."""

    available: bool
    content: str | None = None
    message_id: int | None = None
    created_at: str | None = None
