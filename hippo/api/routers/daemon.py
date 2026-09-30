"""WeChat daemon login endpoints.

Distinct from user authentication: this is the WeChat account the crawler runs
as, not a Hippo user signing in.
"""

import base64
from typing import Any

from fastapi import APIRouter, Depends

from ...exceptions import ApiError
from ...weixin_source import WeixinSource
from ..deps import current_user

router = APIRouter(dependencies=[Depends(current_user)])


@router.get('/login')
async def login_status() -> dict[str, Any]:
    """daemon 登录状态（登录由 weixin-rs daemon 负责，hippo 不再保存凭据）。"""
    try:
        async with WeixinSource(auto_login=False) as source:
            status = await source.status()
    except Exception as exc:
        return {
            'logged_in': False,
            'status': 'unreachable',
            'need_relogin': False,
            'wxid': None,
            'nickname': None,
            'head_url': None,
            'clients_connected': None,
            'error': str(exc),
        }
    logged_in = bool(status.get('logged_in'))
    return {
        'logged_in': logged_in,
        'status': status.get('status') or ('online' if logged_in else 'logged_out'),
        'need_relogin': bool(status.get('need_relogin')),
        'wxid': status.get('wxid'),
        'nickname': status.get('nickname'),
        'head_url': status.get('head_url'),
        'clients_connected': status.get('clients_connected'),
        'error': status.get('error') or '',
    }


@router.post('/login/auto')
async def login_auto() -> dict[str, Any]:
    """用 daemon 本地的 auto_auth_key 免扫重登。"""
    try:
        async with WeixinSource(auto_login=False) as source:
            result = await source.login_auto_now()
    except Exception as exc:
        raise ApiError(f'免扫重登失败：{exc}', status=502) from exc
    return {'ok': True, 'result': result}


@router.post('/login/qr')
async def login_qr() -> dict[str, Any]:
    """索取扫码登录二维码（前端渲染 ``png_base64`` 或 ``url``）。"""
    try:
        async with WeixinSource(auto_login=False) as source:
            qr = await source.start_qr_login()
    except Exception as exc:
        raise ApiError(f'获取二维码失败：{exc}', status=502) from exc
    png = getattr(qr, 'png', b'') or b''
    return {
        'uuid': getattr(qr, 'uuid', ''),
        'url': getattr(qr, 'url', ''),
        'png_base64': base64.b64encode(png).decode('ascii') if png else '',
        'expires_in': int(getattr(qr, 'expires_in', 0) or 0),
    }


@router.post('/login/wait')
async def login_wait() -> dict[str, Any]:
    """等扫码确认（daemon 侧轮询，最长约 5 分钟）。"""
    try:
        async with WeixinSource(auto_login=False) as source:
            result = await source.wait_login()
    except Exception as exc:
        raise ApiError(f'登录等待失败：{exc}', status=502) from exc
    return {'ok': True, 'result': result}
