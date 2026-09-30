import { useState, type FormEvent } from 'react';
import { Link } from 'react-router-dom';
import { useI18n } from '../../i18n';
import { apiSend, ApiError } from '../../api';

export function ForgotPasswordPage() {
  const { t } = useI18n();
  const [email, setEmail] = useState('');
  const [error, setError] = useState('');
  const [submitted, setSubmitted] = useState(false);
  const [pending, setPending] = useState(false);

  const onSubmit = async (event: FormEvent) => {
    event.preventDefault();
    if (pending) return;
    setPending(true);
    setError('');
    try {
      await apiSend('/api/auth/reset-request', 'POST', { email }, { silent: true });
      setSubmitted(true);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : t('forgot.failed', '请求失败。'));
    } finally {
      setPending(false);
    }
  };

  if (submitted) {
    return (
      <div className="auth-screen">
        <div className="auth-card">
          <h1 className="auth-title">{t('forgot.sentTitle', '请查收邮件')}</h1>
          <p className="auth-note">
            {t('forgot.sentBody', '如果 {email} 已注册，你会收到一封重置密码的邮件。').replace('{email}', email)}
          </p>
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
        <h1 className="auth-title">{t('forgot.title', '忘记密码')}</h1>
        <p className="auth-note">{t('forgot.intro', '填写注册邮箱，我们会发送重置链接。')}</p>

        <label className="auth-field">
          <span>{t('register.email', '邮箱')}</span>
          <input
            autoFocus
            autoComplete="email"
            type="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
          />
        </label>

        {error ? <div className="auth-error" role="alert">{error}</div> : null}

        <button className="btn primary auth-submit" type="submit" disabled={pending || !email}>
          {pending ? t('forgot.submitting', '提交中…') : t('forgot.submit', '发送重置链接')}
        </button>

        <Link className="auth-link" to="/login">
          {t('register.backToLogin', '返回登录')}
        </Link>
      </form>
    </div>
  );
}
