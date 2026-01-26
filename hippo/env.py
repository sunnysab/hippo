"""Load environment variables from .env when available."""

from __future__ import annotations

from dotenv import load_dotenv


def load_env() -> None:
    load_dotenv()
