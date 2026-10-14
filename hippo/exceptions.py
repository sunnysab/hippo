"""Application exceptions."""

from __future__ import annotations


class HippoError(RuntimeError):
    """Base exception for all Hippo errors."""


class ApiError(HippoError):
    """An error that maps to an HTTP response.

    ``message`` is the copy the client sees; ``code`` is the stable identifier
    callers branch on. Keeping the two apart stops UI wording from becoming log
    content and alert text, and lets a client localise without parsing prose.
    The cause, when there is one, is logged but never sent.
    """

    def __init__(self, message: str, *, status: int = 400, code: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code or f'http_{status}'


class SyncError(HippoError):
    """Base exception for sync-related errors."""


class SyncInterrupted(SyncError):
    """Raised when sync is cancelled by user."""


class StorageError(HippoError):
    """Raised when database operations fail."""


class StorageInitError(StorageError):
    """Raised when database is not initialized or schema is outdated."""
