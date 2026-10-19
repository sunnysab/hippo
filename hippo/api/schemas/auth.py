"""Sign-in, registration, session and API-key schemas."""

from __future__ import annotations

from pydantic import BaseModel, Field

from .common import RowPayload

# -- requests ----------------------------------------------------------------


class LoginRequest(BaseModel):
    """Credentials for ``POST /api/auth/login``."""

    username: str = Field(description='Account username.')
    password: str = Field(description='Account password.')


class PasswordChangeRequest(BaseModel):
    """``POST /api/auth/password``: the current password is re-confirmed."""

    current_password: str = Field(description='Password in use right now.')
    new_password: str = Field(description='Replacement password, at least 8 characters.')


class EmailChangeRequest(BaseModel):
    """``PATCH /api/auth/email``. An empty ``email`` clears the address."""

    current_password: str = Field(description='Password in use right now.')
    email: str = Field(description='New address, or an empty string to remove it.')


class RegisterRequest(BaseModel):
    """``POST /api/auth/register``."""

    username: str = Field(description='Desired username.')
    email: str = Field(description='Address the verification link is sent to.')
    password: str = Field(description='Password, at least 8 characters.')


class TokenRequest(BaseModel):
    """``POST /api/auth/verify``: consume a verification token."""

    token: str = Field(description='One-shot token from the e-mail link.')


class ResetRequestRequest(BaseModel):
    """``POST /api/auth/reset-request``."""

    email: str = Field(description='Address the reset link is sent to.')


class ResetPasswordRequest(BaseModel):
    """``POST /api/auth/reset``."""

    token: str = Field(description='One-shot token from the e-mail link.')
    password: str = Field(description='New password, at least 8 characters.')


class ApiKeyCreateRequest(BaseModel):
    """``POST /api/apikey``. Omitted fields fall back to the console defaults."""

    name: str | None = Field(default=None, description='Label shown in the key list.')
    scopes: list[str] | None = Field(default=None, description="Subset of the issued scopes; defaults to ['read'].")
    expires_in_days: int | None = Field(default=None, description='Days until expiry; omitted means no expiry.')


# -- responses ---------------------------------------------------------------


class UserOut(BaseModel):
    """The signed-in account, as the SPA holds it."""

    id: int
    username: str
    email: str | None = None
    email_verified: bool = False
    role: str = Field(description='``user`` or ``admin``.')
    timezone: str = Field(description='IANA name, used to bound the daily report.')


class SessionOut(BaseModel):
    """One browser session.

    Closed on purpose, unlike a content row: the stored row carries the token
    hash, and a response model that cannot name a field is the one guarantee
    that it never reaches a client.
    """

    user_agent: str | None = None
    ip: str | None = None
    created_at: str | None = None
    expires_at: str | None = None


class SessionListOut(BaseModel):
    sessions: list[SessionOut]


class ApiKeyOut(RowPayload):
    """One issued key. The secret itself exists only in the creation response."""

    id: int
    name: str
    prefix: str = Field(description='First characters of the secret, for recognition.')
    scopes: list[str]
    last_used_at: str | None = None
    last_used_ip: str | None = None
    expires_at: str | None = None
    created_at: str | None = None


class ApiKeyCreatedOut(ApiKeyOut):
    secret: str = Field(description='The only copy of the secret; it cannot be fetched again.')


class ApiKeyListOut(BaseModel):
    keys: list[ApiKeyOut]
    scopes: list[str] = Field(description='Every scope this instance can issue.')


class RegistrationPendingOut(BaseModel):
    status: str
    email_sent: bool = Field(description='False when SMTP is not configured.')


class AcceptedOut(BaseModel):
    """Answer of a request that is always accepted to avoid probing the account."""

    status: str


class CredentialUserOut(BaseModel):
    """The three identity fields every credential answer carries."""

    id: int
    username: str
    role: str


class WhoamiOut(BaseModel):
    """What the credential that made the request is."""

    via: str = Field(description='``session`` or ``api_key``.')
    user: CredentialUserOut
    scopes: list[str]
    key: dict[str, str | int] | None = Field(default=None, description='Key id and name, when a key was used.')
