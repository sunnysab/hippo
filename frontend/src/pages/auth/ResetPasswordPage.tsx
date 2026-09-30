import { useState, type FormEvent } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { useI18n } from '../../i18n';
import { apiSend, ApiError } from '../../api';

export function ResetPasswordPage() {
  const { t } = useI18n();
  const [params] = useSearchParams();
  const token = params.get('token') ?? '';
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');
  const [done, setDone] = useState(false);
  const [pending, setPending] = useState(false);

  const onSubmit = async (event: FormEvent) => {
    event.preventDefault();
    if (pending) return;
    setPending(true);
    setError('');
    try {
      await apiSend('/api/auth/reset', 'POST', { token, password }, { silent: true });
      setDone(true);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : t('reset.failed', '重置失败。'));
    } finally {
      setPending(false);
    }
  };

  if (done) {
    return (
      <div className="auth-screen">
        <div className="auth-card">
          <h1 className="auth-title">{t('reset.doneTitle', '密码已重置')}</h1>
          <p className="auth-note">{t('reset.doneBody', '所有设备均已退出登录，请用新密码重新登录。')}</p>
          <Link className="btn primary auth-submit" to="/login">
            {t('register.backToLogin', '返回登录')}
          </Link>
        </div>
      </div>
    );
  }

  return (
    <div className="auth-screen">
      <form className="auth-card" onSubmit={onSubmit}>
        <h1 className="auth-title">{t('reset.title', '设置新密码')}</h1>

        <label className="auth-field">
          <span>{t('reset.newPassword', '新密码')}</span>
          <input
            autoFocus
            autoComplete="new-password"
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
          <small>{t('register.passwordHint', '至少 8 位')}</small>
        </label>

        {!token ? (
          <div className="auth-error">{t('reset.missingToken', '链接缺少令牌，请重新申请。')}</div>
        ) : null}
        {error ? <div className="auth-error" role="alert">{error}</div> : null}

        <button
          className="btn primary auth-submit"
          type="submit"
          disabled={pending || !token || password.length < 8}
        >
          {pending ? t('reset.submitting', '提交中…') : t('reset.submit', '重置密码')}
        </button>
      </form>
    </div>
  );
}
