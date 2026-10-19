"""WeChat daemon login schemas.

Distinct from user authentication: ``/api/login/*`` reports and drives the WeChat
account the crawler runs as. The daemon is an external process reached over RPC,
so every field here is a pass-through of what it answers, named once.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class DaemonLoginStatusOut(BaseModel):
    """``GET /api/login``: state of the WeChat session hippo crawls with."""

    logged_in: bool
    status: str = Field(description="online / logged_out / unreachable / the daemon's own word.")
    need_relogin: bool
    wxid: str | None = None
    nickname: str | None = None
    head_url: str | None = None
    clients_connected: int | None = None
    error: str = Field(default='', description='Why the daemon could not be reached, when it could not.')


class DaemonQrOut(BaseModel):
    """``POST /api/login/qr``: what the console renders so the owner can scan."""

    uuid: str
    url: str = ''
    png_base64: str = Field(description='PNG bytes, base64 encoded; empty when the daemon sent only a URL.')
    expires_in: int = Field(description='Seconds until the QR code goes stale.')


class DaemonActionResultOut(BaseModel):
    """``POST /api/login/auto`` and ``POST /api/login/wait``."""

    ok: bool
    result: dict[str, Any] = Field(description="The daemon's own reply, passed through unchanged.")
