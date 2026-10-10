import { useState } from 'react';
import { apiSend, isAuthError } from '../../api';
import { useAuth } from '../../hooks/useAuth';
import { useToast } from '../../hooks/useToast';
import { useI18n } from '../../i18n';

/** Self-service profile: the address this account's mail is delivered to. */
export function AccountPanel() {
  const { user, refresh } = useAuth();
  const { t } = useI18n();
  const { showToast } = useToast();
  const [email, setEmail] = useState(user?.email ?? '');
  const [password, setPassword] = useState('');
  const [saving, setSaving] = useState(false);

  const save = async () => {
    if (!password) {
      showToast(t('account.passwordRequired', '请输入当前密码。'));
      return;
    }
    setSaving(true);
    try {
      await apiSend('/api/auth/email', 'PATCH', { email: email.trim(), current_password: password });
      await refresh();
      setPassword('');
      showToast(t('account.saved', '邮箱已更新。'));
    } catch (err) {
      if (isAuthError(err)) return;
      showToast((err as Error)?.message || t('account.saveFailed', '保存失败。'));
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="panel">
      <div className="panel-header">
        <div>
          <h2>{t('account.title', '账号')}</h2>
          <p className="muted">{t('account.subtitle', '修改接收系统邮件的邮箱地址。')}</p>
        </div>
        <div className="toolbar">
          <button className="btn" type="button" disabled={saving} onClick={() => void save()}>
            {saving ? t('account.saving', '保存中…') : t('common.save', '保存')}
          </button>
        </div>
      </div>
      <div className="form-grid">
        <label className="auth-field">
          <span>{t('account.username', '用户名')}</span>
          <input className="input" value={user?.username ?? ''} readOnly disabled />
        </label>
        <label className="auth-field">
          <span>{t('account.email', '邮箱')}</span>
          <input
            className="input"
            type="email"
            autoComplete="email"
            value={email}
            onChange={(event) => setEmail(event.target.value)}
          />
          <small className="muted">{t('account.emailHint', '留空则不接收邮件。')}</small>
        </label>
        <label className="auth-field">
          <span>{t('account.currentPassword', '当前密码')}</span>
          <input
            className="input"
            type="password"
            autoComplete="current-password"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
          />
        </label>
      </div>
    </div>
  );
}
