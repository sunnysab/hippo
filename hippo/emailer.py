"""Email helpers for sync alerts."""

from __future__ import annotations

import smtplib
from email.message import EmailMessage
from typing import Any

from .storage import PostgresStorage
from .utils import load_meta_json, save_meta_json

_EMAIL_SETTINGS_KEY = 'email:settings'


def default_email_settings() -> dict[str, Any]:
    return {
        'smtp_host': '',
        'smtp_port': 587,
        'smtp_user': '',
        'smtp_password': '',
        'smtp_tls': True,
        'from_email': '',
    }


def get_email_settings(storage: PostgresStorage) -> dict[str, Any]:
    settings = load_meta_json(storage, _EMAIL_SETTINGS_KEY, default_email_settings())
    defaults = default_email_settings()
    return {**defaults, **(settings or {})}


def set_email_settings(storage: PostgresStorage, updates: dict[str, Any]) -> dict[str, Any]:
    current = get_email_settings(storage)
    current.update(updates)
    save_meta_json(storage, _EMAIL_SETTINGS_KEY, current)
    return current


def send_email(settings: dict[str, Any], *, to_email: str, subject: str, body: str) -> None:
    if not to_email:
        return
    smtp_host = settings.get('smtp_host') or ''
    if not smtp_host:
        return
    message = EmailMessage()
    message['Subject'] = subject
    message['From'] = settings.get('from_email') or settings.get('smtp_user') or to_email
    message['To'] = to_email
    message.set_content(body)
    smtp_port = int(settings.get('smtp_port') or 587)
    smtp_user = settings.get('smtp_user')
    smtp_password = settings.get('smtp_password')
    use_tls = bool(settings.get('smtp_tls'))
    with smtplib.SMTP(smtp_host, smtp_port, timeout=15) as smtp:
        if use_tls:
            smtp.starttls()
        if smtp_user:
            smtp.login(smtp_user, smtp_password or '')
        smtp.send_message(message)
