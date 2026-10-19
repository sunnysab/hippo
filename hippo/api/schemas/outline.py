"""Article navigation outline requests and responses."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from ...outline import OutlineSection


class OutlineRequest(BaseModel):
    force: bool = Field(default=False, strict=True)
    content_updated_at: datetime | None = None


class OutlineOut(BaseModel):
    state: Literal['missing', 'stale', 'queued', 'processing', 'ready', 'failed']
    content_updated_at: datetime | None = None
    sections: list[OutlineSection] = Field(default_factory=list)
    model: str | None = None
    updated_at: datetime | None = None
    error_code: str | None = None
    can_generate: bool
