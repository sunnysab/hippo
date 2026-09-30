import { useState, type FormEvent } from 'react';
import { Link } from 'react-router-dom';
import { useI18n } from '../../i18n';
import { useAuth } from '../../hooks/useAuth';
import { ApiError } from '../../api';

export function LoginPage() {
  const { t } = useI18n();
  const { signIn } = useAuth();
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');
  const [pending, setPending] = useState(false);

  const onSubmit = async (event: FormEvent) => {
    event.preventDefault();
    if (pending) return;
    setPending(true);
    setError('');
    try {
      await signIn(username, password);
      window.location.hash = '#/groups';
    } catch (err) {
      setError(err instanceof ApiError ? err.message : t('login.failed', 'Sign in failed.'));
    } finally {
      setPending(false);
    }
  };

  return (
    <div className="auth-screen">
      <form className="auth-card" onSubmit={onSubmit}>
        <div className="auth-brand">
          <span className="brand-mark">H</span>
          <div>
            <div className="brand-title">Hippo</div>
            <div className="brand-subtitle">{t('brand.subtitle', '公众号文章管理')}</div>
          </div>
        </div>

        <label className="auth-field">
          <span>{t('login.username', '用户名')}</span>
          <input
            autoFocus
            autoComplete="username"
            name="username"
            value={username}
            onChange={(event) => setUsername(event.target.value)}
          />
        </label>

        <label className="auth-field">
          <span>{t('login.password', '密码')}</span>
          <input
            autoComplete="current-password"
            name="password"
            type="password"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
          />
        </label>

        {error ? <div className="auth-error" role="alert">{error}</div> : null}

        <button className="btn primary auth-submit" type="submit" disabled={pending || !username || !password}>
          {pending ? t('login.signingIn', '登录中…') : t('login.submit', '登录')}
        </button>

        <div className="auth-links">
          <Link className="auth-link" to="/forgot">
            {t('login.forgot', '忘记密码？')}
          </Link>
          <Link className="auth-link" to="/register">
            {t('login.register', '注册账号')}
          </Link>
        </div>
      </form>
    </div>
  );
}
