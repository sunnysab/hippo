import { useCallback, useEffect, useState } from 'react';
import { apiGet, apiSend, isAuthError } from '../../api';
import { useI18n } from '../../i18n';
import { useToast } from '../../hooks/useToast';

type CredentialState = Record<string, { configured: boolean }>;

/**
 * Per-user source credentials.
 *
 * Bilibili's listing endpoint is risk-controlled, so it needs a session cookie. The
 * instance has one (the admin sets it); this is for a reader who wants their own,
 * which is used for what they trigger from here.
 */
export function SourcesPanel() {
  const { t } = useI18n();
  const { showToast } = useToast();
  const [credentials, setCredentials] = useState<CredentialState>({});
  const [cookie, setCookie] = useState('');
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);

  const load = useCallback(async () => {
    const payload = await apiGet('/api/source/credential') as unknown as CredentialState;
    setCredentials(payload || {});
  }, []);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const payload = await apiGet('/api/source/credential') as unknown as CredentialState;
        if (!cancelled) setCredentials(payload || {});
      } catch (error) {
        if (!isAuthError(error)) showToast((error as Error)?.message || t('sources.credentialLoadFailed', '读取失败。'));
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [showToast, t]);

  const save = async () => {
    const secret = cookie.trim();
    if (!secret) return;
    setSaving(true);
    try {
      await apiSend('/api/source/credential/bilibili', 'PUT', { secret });
      setCookie('');
      await load();
      showToast(t('sources.credentialSaved', '已保存。'));
    } catch (error) {
      if (!isAuthError(error)) showToast((error as Error)?.message || t('sources.credentialSaveFailed', '保存失败。'));
    } finally {
      setSaving(false);
    }
  };

  const clear = async () => {
    setSaving(true);
    try {
      await apiSend('/api/source/credential/bilibili', 'DELETE', {});
      await load();
      showToast(t('sources.credentialCleared', '已清除。'));
    } catch (error) {
      if (!isAuthError(error)) showToast((error as Error)?.message || t('sources.credentialSaveFailed', '保存失败。'));
    } finally {
      setSaving(false);
    }
  };

  if (loading) {
    return <div className="panel muted">{t('admin.loading', '加载中…')}</div>;
  }

  const configured = Boolean(credentials.bilibili?.configured);

  return (
    <div className="panel">
      <div className="panel-header">
        <div>
          <h2>{t('sources.credentialTitle', '我的来源凭证')}</h2>
          <p className="muted">
            {t(
              'sources.credentialSubtitle',
              'B站投稿列表需要 SESSDATA。这里配置自己的；管理台配的是全实例通用的那一份。',
            )}
          </p>
        </div>
      </div>

      <div className="form-grid">
        <label className="auth-field">
          <span>
            {t('sources.bilibiliCookie', 'B站 Cookie')}
            {configured ? ` · ${t('sources.credentialSet', '已配置')}` : ''}
          </span>
          <input
            className="input"
            id="source-credential"
            type="password"
            autoComplete="off"
            placeholder={configured ? t('sources.credentialReplace', '留空则不改，输入新值覆盖') : 'SESSDATA=…'}
            value={cookie}
            onChange={(event) => setCookie(event.target.value)}
          />
        </label>
      </div>

      <div className="toolbar">
        <button className="btn" id="source-credential-save" type="button" disabled={saving || !cookie.trim()} onClick={() => void save()}>
          {saving ? t('common.saving', '保存中…') : t('common.save', '保存')}
        </button>
        {configured ? (
          <button className="btn ghost" id="source-credential-clear" type="button" disabled={saving} onClick={() => void clear()}>
            {t('sources.credentialClear', '清除')}
          </button>
        ) : null}
      </div>

      <p className="muted admin-note">
        {t('sources.credentialNote', '凭证只用于你自己触发的操作（解析、订阅）；后台同步用实例级的那一份。')}
      </p>
    </div>
  );
}
