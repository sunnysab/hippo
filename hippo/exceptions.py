"""Application exceptions."""

from __future__ import annotations


class ApiError(RuntimeError):
    def __init__(self, message: str, *, status: int = 400) -> None:
        super().__init__(message)
        self.status = status
