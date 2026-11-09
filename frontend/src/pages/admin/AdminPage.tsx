import { useCallback, useEffect, useState } from 'react';
import { apiGet, apiSend } from '../../api';
import { useI18n } from '../../i18n';
import { useToast } from '../../hooks/useToast';
import { useAuth } from '../../hooks/useAuth';
import { ConfirmModal } from '../../components/ConfirmModal';
import { formatDateTime } from '../../utils/format';

interface AdminUser {
  id: number;
  username: string;
  email: string | null;
  email_verified: boolean;
  role: string;
  timezone: string;
  is_disabled: boolean;
  created_at: string | null;
  session_count: number;
  last_login_at: string | null;
}

const formatTime = (value: string | null) => formatDateTime(value) || '—';

export function AdminPage() {
  const { t } = useI18n();
  const { showToast } = useToast();
  const { user: actor } = useAuth();
  const [users, setUsers] = useState<AdminUser[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState('');
  const [creating, setCreating] = useState(false);
  const [draft, setDraft] = useState({ username: '', email: '', password: '', role: 'user' });
  const [confirmation, setConfirmation] = useState<{ user: AdminUser; action: 'disable' | 'delete' } | null>(null);

  const load = useCallback(async () => {
    try {
      const payload = await apiGet('/api/admin/user');
      setUsers((payload.items as AdminUser[]) ?? []);
      setLoadError('');
    } catch (error) {
      setLoadError(error instanceof Error ? error.message : String(error));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    let cancelled = false;
    queueMicrotask(() => { if (!cancelled) void load(); });
    return () => { cancelled = true; };
  }, [load]);

  const createUser = async () => {
    if (creating) return;
    if (!draft.username.trim() || !draft.password) {
      showToast(t('admin.user.missing', '用户名和密码不能为空'));
      return;
    }
    setCreating(true);
    try {
      await apiSend('/api/admin/user', 'POST', {
        username: draft.username.trim(),
        email: draft.email.trim() || null,
        password: draft.password,
        role: draft.role,
      });
      showToast(t('admin.user.created', '用户已创建'));
      setDraft({ username: '', email: '', password: '', role: 'user' });
      await load();
    } catch (error) {
      showToast(error instanceof Error ? error.message : String(error));
    } finally {
      setCreating(false);
    }
  };

  const patchUser = async (target: AdminUser, body: Record<string, unknown>) => {
    try {
      await apiSend(`/api/admin/user/${target.id}`, 'PATCH', body);
      await load();
    } catch (error) {
      showToast(error instanceof Error ? error.message : String(error));
    }
  };

  const resetPassword = async (target: AdminUser) => {
    const password = window.prompt(
      t('admin.user.newPassword', '输入 {name} 的新密码（至少 8 位）').replace('{name}', target.username),
    );
    if (!password) return;
    try {
      await apiSend(`/api/admin/user/${target.id}/password`, 'POST', { password });
      showToast(t('admin.user.passwordReset', '密码已重置，该用户的会话已全部吊销'));
      await load();
    } catch (error) {
      showToast(error instanceof Error ? error.message : String(error));
    }
  };

  const revokeSessions = async (target: AdminUser) => {
    try {
      await apiSend(`/api/admin/user/${target.id}/session`, 'DELETE', {});
      showToast(t('admin.user.sessionsRevoked', '会话已吊销'));
      await load();
    } catch (error) {
      showToast(error instanceof Error ? error.message : String(error));
    }
  };

  const confirmAction = async () => {
    if (!confirmation) return;
    const { user, action } = confirmation;
    if (action === 'delete') {
      await apiSend(`/api/admin/user/${user.id}`, 'DELETE', {});
      showToast(t('admin.user.deleted', '用户已删除'));
    } else {
      await apiSend(`/api/admin/user/${user.id}`, 'PATCH', { is_disabled: true });
    }
    await load();
  };

  return (
    <div className="admin-page">
      <section className="panel">
        <div className="panel-header">
          <h2 className="panel-title">{t('admin.user.title', '用户管理')}</h2>
          {!loading && !loadError && <span className="muted">{t('admin.user.count', '{n} 个账号').replace('{n}', String(users.length))}</span>}
        </div>

        {loading ? (
          <div className="muted admin-empty">{t('admin.loading', '加载中…')}</div>
        ) : loadError ? (
          <div className="admin-empty" role="alert">
            <p>{loadError}</p>
            <button className="btn ghost" type="button" onClick={() => void load()}>{t('common.retry', '重试')}</button>
          </div>
        ) : (
          <div className="admin-table-wrap">
            <table className="admin-table">
              <thead>
                <tr>
                  <th>{t('admin.user.username', '用户名')}</th>
                  <th>{t('admin.user.email', '邮箱')}</th>
                  <th>{t('admin.user.role', '角色')}</th>
                  <th>{t('admin.user.sessions', '会话')}</th>
                  <th>{t('admin.user.lastLogin', '最后登录')}</th>
                  <th>{t('admin.user.actions', '操作')}</th>
                </tr>
              </thead>
              <tbody>
                {users.map((row) => {
                  const isSelf = row.id === actor?.id;
                  return (
                    <tr key={row.id} className={row.is_disabled ? 'is-disabled' : ''}>
                      <td>
                        {row.username}
                        {isSelf ? <span className="admin-badge">{t('admin.user.you', '你')}</span> : null}
                        {row.is_disabled ? (
                          <span className="admin-badge admin-badge-warn">
                            {t('admin.user.disabled', '已禁用')}
                          </span>
                        ) : null}
                      </td>
                      <td>
                        {row.email ?? '—'}
                        {row.email ? (
                          <span className={`admin-badge ${row.email_verified ? '' : 'admin-badge-warn'}`}>
                            {row.email_verified
                              ? t('admin.user.verified', '已验证')
                              : t('admin.user.unverified', '未验证')}
                          </span>
                        ) : null}
                      </td>
                      <td>
                        <select
                          className="input"
                          value={row.role}
                          disabled={isSelf}
                          aria-label={t('admin.user.roleFor', '{name} 的角色').replace('{name}', row.username)}
                          onChange={(event) => void patchUser(row, { role: event.target.value })}
                        >
                          <option value="user">{t('admin.user.roleUser', '普通用户')}</option>
                          <option value="admin">{t('admin.user.roleAdmin', '管理员')}</option>
                        </select>
                      </td>
                      <td>{row.session_count}</td>
                      <td>{formatTime(row.last_login_at)}</td>
                      <td className="admin-actions">
                        {row.email && !row.email_verified ? (
                          <button
                            className="btn ghost"
                            type="button"
                            onClick={() => void patchUser(row, { email_verified: true })}
                          >
                            {t('admin.user.verify', '标记已验证')}
                          </button>
                        ) : null}
                        <button
                          className="btn ghost"
                          type="button"
                          disabled={isSelf}
                          title={isSelf ? t('admin.user.selfProtected', '不能禁用或删除自己的账户') : undefined}
                          onClick={() =>
                            row.is_disabled
                              ? void patchUser(row, { is_disabled: false })
                              : setConfirmation({ user: row, action: 'disable' })
                          }
                        >
                          {row.is_disabled
                            ? t('admin.user.enable', '启用')
                            : t('admin.user.disable', '禁用')}
                        </button>
                        <button
                          className="btn ghost"
                          type="button"
                          disabled={isSelf}
                          title={isSelf ? t('admin.user.selfProtected', '不能禁用或删除自己的账户') : undefined}
                          onClick={() => setConfirmation({ user: row, action: 'delete' })}
                        >
                          {t('common.delete', '删除')}
                        </button>
                        <button className="btn ghost" type="button" onClick={() => void resetPassword(row)}>
                          {t('admin.user.reset', '重置密码')}
                        </button>
                        <button
                          className="btn ghost"
                          type="button"
                          disabled={row.session_count === 0}
                          onClick={() => void revokeSessions(row)}
                        >
                          {t('admin.user.revoke', '吊销会话')}
                        </button>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <section className="panel">
        <div className="panel-header">
          <h2 className="panel-title">{t('admin.create.title', '新建用户')}</h2>
        </div>
        <div className="form-grid">
          <label className="auth-field">
            <span>{t('admin.user.username', '用户名')}</span>
            <input
              className="input"
              value={draft.username}
              onChange={(event) => setDraft({ ...draft, username: event.target.value })}
            />
          </label>
          <label className="auth-field">
            <span>{t('admin.user.email', '邮箱')}</span>
            <input
              className="input"
              type="email"
              value={draft.email}
              onChange={(event) => setDraft({ ...draft, email: event.target.value })}
            />
          </label>
          <label className="auth-field">
            <span>{t('admin.user.password', '初始密码')}</span>
            <input
              className="input"
              type="password"
              autoComplete="new-password"
              value={draft.password}
              onChange={(event) => setDraft({ ...draft, password: event.target.value })}
            />
          </label>
          <label className="auth-field">
            <span>{t('admin.user.role', '角色')}</span>
            <select
              className="input"
              value={draft.role}
              onChange={(event) => setDraft({ ...draft, role: event.target.value })}
            >
              <option value="user">{t('admin.user.roleUser', '普通用户')}</option>
              <option value="admin">{t('admin.user.roleAdmin', '管理员')}</option>
            </select>
          </label>
        </div>
        <div className="panel-footer">
          <button className="btn" type="button" disabled={creating} onClick={() => void createUser()}>
            {creating ? t('admin.create.submitting', '创建中…') : t('admin.create.submit', '创建')}
          </button>
          <span className="muted">
            {t('admin.create.hint', '邮箱留空则只能由管理员重置密码')}
          </span>
        </div>
      </section>

      <ConfirmModal
        isOpen={confirmation !== null}
        title={confirmation?.action === 'delete' ? t('admin.user.deleteTitle', '删除用户') : t('admin.user.disableTitle', '禁用账号')}
        message={(confirmation?.action === 'delete' ? t(
          'admin.user.deleteConfirm',
          '将永久删除用户 {name} 及其订阅、阅读记录、收藏、批注、会话和知识库记录，无法恢复。共享文章和图片会保留。确定删除？',
        ) : t(
          'admin.user.disableConfirm',
          '禁用 {name} 会立即吊销其全部会话，确定继续？',
        )).replace('{name}', confirmation?.user.username ?? '')}
        confirmLabel={confirmation?.action === 'delete' ? t('common.delete', '删除') : t('admin.user.disable', '禁用')}
        onConfirm={confirmAction}
        onClose={() => setConfirmation(null)}
      />
    </div>
  );
}
