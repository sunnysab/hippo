import { useEffect, useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { useI18n } from '../../i18n';
import { apiSend } from '../../api';

type VerifyState = 'pending' | 'ok' | 'failed';

export function VerifyPage() {
  const { t } = useI18n();
  const [params] = useSearchParams();
  const token = params.get('token') ?? '';
  // Derived from the token so the effect only ever performs the request.
  const [state, setState] = useState<VerifyState>(token ? 'pending' : 'failed');

  useEffect(() => {
    if (!token) return;
    const kickoff = window.setTimeout(() => {
      apiSend('/api/auth/verify', 'POST', { token }, { silent: true })
        .then(() => setState('ok'))
        .catch(() => setState('failed'));
    }, 0);
    return () => window.clearTimeout(kickoff);
  }, [token]);

  const copy = {
    pending: { title: t('verify.pending', '正在验证…'), body: '' },
    ok: {
      title: t('verify.okTitle', '邮箱验证成功'),
      body: t('verify.okBody', '现在可以登录了。'),
    },
    failed: {
      title: t('verify.failedTitle', '验证失败'),
      body: t('verify.failedBody', '链接无效或已过期，请重新注册或联系管理员。'),
    },
  }[state];

  return (
    <div className="auth-screen">
      <div className="auth-card">
        <h1 className="auth-title">{copy.title}</h1>
        {copy.body ? <p className="auth-note">{copy.body}</p> : null}
        {state !== 'pending' ? (
          <Link className="btn primary auth-submit" to="/login">
            {t('register.backToLogin', '返回登录')}
          </Link>
        ) : null}
      </div>
    </div>
  );
}
