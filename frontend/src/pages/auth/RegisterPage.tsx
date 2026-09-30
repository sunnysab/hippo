import { useState, type FormEvent } from 'react';
import { Link } from 'react-router-dom';
import { useI18n } from '../../i18n';
import { apiSend, ApiError } from '../../api';

export function RegisterPage() {
  const { t } = useI18n();
  const [username, setUsername] = useState('');
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');
  const [submitted, setSubmitted] = useState(false);
  const [mailSent, setMailSent] = useState(false);
  const [pending, setPending] = useState(false);

  const onSubmit = async (event: FormEvent) => {
    event.preventDefault();
    if (pending) return;
    setPending(true);
    setError('');
    try {
      const payload = await apiSend(
        '/api/auth/register',
        'POST',
        { username, email, password },
        { silent: true },
      );
      setMailSent(Boolean(payload.email_sent));
      setSubmitted(true);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : t('register.failed', '注册失败。'));
    } finally {
      setPending(false);
    }
  };

  if (submitted) {
    return (
      <div className="auth-screen">
        <div className="auth-card">
          <h1 className="auth-title">{t('register.pendingTitle', '请验证邮箱')}</h1>
          <p className="auth-note">
            {mailSent
              ? t('register.pendingBody', '我们已向 {email} 发送验证链接，请在 24 小时内完成验证。').replace(
                  '{email}',
                  email,
                )
              : t(
                  'register.mailUnavailable',
                  '账号已创建，但本实例尚未配置邮件服务，请联系管理员协助完成验证。',
                )}
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
        <div className="auth-brand">
          <span className="brand-mark">H</span>
          <div>
            <div className="brand-title">Hippo</div>
            <div className="brand-subtitle">{t('register.subtitle', '创建账号')}</div>
          </div>
        </div>

        <label className="auth-field">
          <span>{t('login.username', '用户名')}</span>
          <input autoFocus autoComplete="username" value={username} onChange={(e) => setUsername(e.target.value)} />
        </label>

        <label className="auth-field">
          <span>{t('register.email', '邮箱')}</span>
          <input autoComplete="email" type="email" value={email} onChange={(e) => setEmail(e.target.value)} />
        </label>

        <label className="auth-field">
          <span>{t('login.password', '密码')}</span>
          <input
            autoComplete="new-password"
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
          <small>{t('register.passwordHint', '至少 8 位')}</small>
        </label>

        {error ? <div className="auth-error" role="alert">{error}</div> : null}

        <button
          className="btn primary auth-submit"
          type="submit"
          disabled={pending || !username || !email || password.length < 8}
        >
          {pending ? t('register.submitting', '提交中…') : t('register.submit', '注册')}
        </button>

        <Link className="auth-link" to="/login">
          {t('register.haveAccount', '已有账号？去登录')}
        </Link>
      </form>
    </div>
  );
}
