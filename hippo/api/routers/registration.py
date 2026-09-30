"""Self-service registration, e-mail verification and password reset.

These endpoints are deliberately public: they run before a session exists.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from fastapi import APIRouter, Body, Depends, Request, status

from ...emailer import get_email_settings, send_email
from ...exceptions import ApiError
from ...logger import get_logger
from ...repositories.token import RESET_PASSWORD, RESET_TTL, VERIFY_EMAIL, VERIFY_TTL
from ...security import hash_password
from ...site_settings import get_site_settings
from ...storage import PostgresStorage
from ..deps import client_ip, get_storage
from ..ratelimit import SlidingWindowLimiter

logger = get_logger(__name__)
router = APIRouter()

#: Registration and password-reset requests are cheap to spam.
_send_limiter = SlidingWindowLimiter(limit=5, window_seconds=900)

_MIN_PASSWORD_LENGTH = 8
_EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')


def _public_base_url(request: Request) -> str:
    """Fallback for instances where ``public_base_url`` is not configured."""
    return str(request.base_url).rstrip('/')


async def _mail(
    storage: PostgresStorage,
    *,
    to_email: str,
    subject: str,
    body: str,
    html: str,
) -> bool:
    """Send an account e-mail. Returns ``False`` when SMTP is unconfigured."""
    settings = await get_email_settings(storage)
    if not str(settings.get('smtp_host') or '').strip():
        logger.warning('SMTP is not configured; cannot send %s', subject)
        return False
    await asyncio.to_thread(
        send_email, settings, to_email=to_email, subject=subject, body=body, html=html
    )
    return True


def _render_mail(*, site_name: str, title: str, intro: str, link: str, action: str, footer: str) -> tuple[str, str]:
    """Return ``(text, html)`` for a single-call-to-action account e-mail."""
    text = f'{title}\n\n{intro}\n\n{action}: {link}\n\n{footer}\n'
    html = f"""<!doctype html>
<html><body style="font-family:system-ui,-apple-system,'PingFang SC',sans-serif;color:#111;line-height:1.6">
  <p style="font-size:13px;color:#6b6b6b;margin:0 0 18px">{site_name}</p>
  <h1 style="font-size:18px;margin:0 0 12px">{title}</h1>
  <p style="margin:0 0 20px">{intro}</p>
  <p style="margin:0 0 20px">
    <a href="{link}" style="display:inline-block;padding:10px 18px;background:#111;color:#fff;
       text-decoration:none;border-radius:2px">{action}</a>
  </p>
  <p style="margin:0 0 8px;font-size:13px;color:#6b6b6b">若按钮无法点击，请复制链接到浏览器：</p>
  <p style="margin:0 0 20px;font-size:13px;word-break:break-all">{link}</p>
  <p style="margin:0;font-size:12px;color:#9a9a9a">{footer}</p>
</body></html>"""
    return text, html


@router.post('/auth/register', status_code=status.HTTP_202_ACCEPTED)
async def register(
    request: Request,
    body: dict[str, Any] = Body(default={}),
    storage: PostgresStorage = Depends(get_storage),
) -> dict[str, Any]:
    """Create an unverified account and mail a verification link."""
    site = await get_site_settings(storage)
    if not site.get('registration_enabled'):
        raise ApiError('当前未开放注册', status=403)

    ip = client_ip(request)
    if not _send_limiter.allow(ip or 'unknown'):
        raise ApiError('请求过于频繁，请稍后再试', status=429)

    username = str(body.get('username') or '').strip()
    email = str(body.get('email') or '').strip()
    password = str(body.get('password') or '')

    if not username:
        raise ApiError('用户名不能为空', status=400)
    if not _EMAIL_RE.match(email):
        raise ApiError('邮箱格式不正确', status=400)
    if len(password) < _MIN_PASSWORD_LENGTH:
        raise ApiError(f'密码至少 {_MIN_PASSWORD_LENGTH} 位', status=400)

    if await storage.users.get_with_password(username) is not None:
        raise ApiError('用户名已被占用', status=409)
    if await storage.users.get_by_email(email) is not None:
        raise ApiError('邮箱已被注册', status=409)

    password_hash = await asyncio.to_thread(hash_password, password)
    async with storage.transaction():
        user = await storage.users.create(
            username=username, password_hash=password_hash, email=email
        )
        token = await storage.tokens.issue(user.id, VERIFY_EMAIL, ttl=VERIFY_TTL)
        await storage.audit.record(user.id, 'auth.registered', target=username, ip=ip)

    base = str(site.get('public_base_url') or '').rstrip('/') or _public_base_url(request)
    link = f'{base}/#/verify?token={token}'
    text, html = _render_mail(
        site_name=str(site.get('site_name') or 'Hippo'),
        title='验证你的邮箱',
        intro=f'你好 {username}，请点击下面的链接完成注册：',
        link=link,
        action='验证邮箱',
        footer='如果这不是你本人的操作，请忽略这封邮件。',
    )
    sent = await _mail(storage, to_email=email, subject='验证你的 Hippo 账号', body=text, html=html)
    return {'status': 'pending_verification', 'email_sent': sent}


@router.post('/auth/verify', status_code=status.HTTP_204_NO_CONTENT)
async def verify(
    body: dict[str, Any] = Body(default={}),
    storage: PostgresStorage = Depends(get_storage),
) -> None:
    """Consume a verification token."""
    token = str(body.get('token') or '').strip()
    user_id = await storage.tokens.consume(token, VERIFY_EMAIL)
    if user_id is None:
        raise ApiError('链接无效或已过期', status=400)
    async with storage.transaction():
        await storage.users.set_email_verified(user_id)
        await storage.audit.record(user_id, 'auth.email_verified')


@router.post('/auth/reset-request', status_code=status.HTTP_202_ACCEPTED)
async def reset_request(
    request: Request,
    body: dict[str, Any] = Body(default={}),
    storage: PostgresStorage = Depends(get_storage),
) -> dict[str, Any]:
    """Mail a password-reset link.

    Always reports success so the endpoint cannot be used to probe which
    addresses are registered.
    """
    email = str(body.get('email') or '').strip()
    ip = client_ip(request)
    if not _send_limiter.allow(ip or 'unknown'):
        raise ApiError('请求过于频繁，请稍后再试', status=429)
    if not _EMAIL_RE.match(email):
        raise ApiError('邮箱格式不正确', status=400)

    user = await storage.users.get_by_email(email)
    if user is None or user.is_disabled:
        return {'status': 'accepted'}

    site = await get_site_settings(storage)
    async with storage.transaction():
        token = await storage.tokens.issue(user.id, RESET_PASSWORD, ttl=RESET_TTL)
        await storage.audit.record(user.id, 'auth.reset_requested', target=user.username, ip=ip)

    base = str(site.get('public_base_url') or '').rstrip('/') or _public_base_url(request)
    link = f'{base}/#/reset?token={token}'
    text, html = _render_mail(
        site_name=str(site.get('site_name') or 'Hippo'),
        title='重置密码',
        intro=f'你好 {user.username}，请点击下面的链接设置新密码（1 小时内有效）：',
        link=link,
        action='重置密码',
        footer='如果这不是你本人的操作，请忽略这封邮件，你的密码不会被修改。',
    )
    await _mail(storage, to_email=email, subject='重置你的 Hippo 密码', body=text, html=html)
    return {'status': 'accepted'}


@router.post('/auth/reset', status_code=status.HTTP_204_NO_CONTENT)
async def reset(
    body: dict[str, Any] = Body(default={}),
    storage: PostgresStorage = Depends(get_storage),
) -> None:
    """Set a new password using a reset token, invalidating every session."""
    token = str(body.get('token') or '').strip()
    password = str(body.get('password') or '')
    if len(password) < _MIN_PASSWORD_LENGTH:
        raise ApiError(f'密码至少 {_MIN_PASSWORD_LENGTH} 位', status=400)

    user_id = await storage.tokens.consume(token, RESET_PASSWORD)
    if user_id is None:
        raise ApiError('链接无效或已过期', status=400)

    password_hash = await asyncio.to_thread(hash_password, password)
    async with storage.transaction():
        await storage.users.set_password(user_id, password_hash)
        await storage.sessions.revoke_all_for_user(user_id)
        await storage.audit.record(user_id, 'auth.password_reset')
