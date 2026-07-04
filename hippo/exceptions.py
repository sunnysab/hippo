"""Application exceptions."""

from __future__ import annotations


class HippoError(RuntimeError):
    """Base exception for all Hippo errors."""


class ApiError(HippoError):
    def __init__(self, message: str, *, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


class SyncError(HippoError):
    """Base exception for sync-related errors."""


class SyncInterrupted(SyncError):
    """Raised when sync is cancelled by user."""


class StorageError(HippoError):
    """Raised when database operations fail."""


class StorageInitError(StorageError):
    """Raised when database is not initialized or schema is outdated."""
