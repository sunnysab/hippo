import { useCallback, useEffect, useState } from 'react';
import { apiGet, apiSend, isAuthError } from '../../api';
import { useI18n } from '../../i18n';
import { useToast } from '../../hooks/useToast';
import { getSourceLabel } from '../../utils/source';

type CredentialState = Record<string, { configured: boolean }>;
type Probe = { ok: boolean; detail: string; account: string; credential: string };

/** The sources hippo accepts personal credentials for. */
const SOURCES = ['bilibili', 'youtube', 'caixin'];

/**
 * Per-user source credentials.
 *
 * These sources use cookies for listing, bot checks or paid-article access.
 * Personal credentials take precedence over the instance credential.
 */
export function SourcesPanel() {
  const { t } = useI18n();
  const { showToast } = useToast();
  const [credentials, setCredentials] = useState<CredentialState>({});
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [probes, setProbes] = useState<Record<string, Probe>>({});
  const [busy, setBusy] = useState('');
  const [loading, setLoading] = useState(true);

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

  const save = async (source: string) => {
    const secret = (drafts[source] || '').trim();
    if (!secret) return;
    setBusy(source);
    try {
      await apiSend(`/api/source/credential/${source}`, 'PUT', { secret });
      setDrafts((previous) => ({ ...previous, [source]: '' }));
      await load();
      showToast(t('sources.credentialSaved', '已保存。'));
    } catch (error) {
      if (!isAuthError(error)) showToast((error as Error)?.message || t('sources.credentialSaveFailed', '保存失败。'));
    } finally {
      setBusy('');
    }
  };

  const clear = async (source: string) => {
    setBusy(source);
    try {
      await apiSend(`/api/source/credential/${source}`, 'DELETE', {});
      setProbes((previous) => ({ ...previous, [source]: undefined as unknown as Probe }));
      await load();
      showToast(t('sources.credentialCleared', '已清除。'));
    } catch (error) {
      if (!isAuthError(error)) showToast((error as Error)?.message || t('sources.credentialSaveFailed', '保存失败。'));
    } finally {
      setBusy('');
    }
  };

  /** Ask the source what it makes of the credential, and show the account it named. */
  const probe = async (source: string) => {
    setBusy(source);
    try {
      const answer = await apiSend(`/api/source/credential/${source}/test?scope=user`, 'POST', {}) as unknown as Probe;
      setProbes((previous) => ({ ...previous, [source]: answer }));
    } catch (error) {
      if (!isAuthError(error)) showToast((error as Error)?.message || t('sources.credentialTestFailed', '测试失败。'));
    } finally {
      setBusy('');
    }
  };

  if (loading) {
    return <div className="panel muted">{t('admin.loading', '加载中…')}</div>;
  }

  return (
    <div className="panel">
      <div className="panel-header">
        <div>
          <h2>{t('sources.credentialTitle', '我的来源凭证')}</h2>
          <p className="muted">
            {t(
              'sources.credentialSubtitle',
              '个人凭证用于你订阅的频道；管理员在管理台维护实例凭证。',
            )}
          </p>
        </div>
      </div>

      {SOURCES.map((source) => {
        const configured = Boolean(credentials[source]?.configured);
        const answer = probes[source];
        return (
          <div className="credential-block" key={source}>
            <label className="auth-field">
              <span>
                {`${getSourceLabel(source, t)} · ${configured ? t('sources.credentialSet', '已配置') : t('sources.credentialUnset', '未配置')}`}
              </span>
              {source === 'youtube' ? (
                <textarea
                  className="input"
                  id={`source-credential-${source}`}
                  rows={3}
                  spellCheck={false}
                  value={drafts[source] || ''}
                  placeholder={configured ? t('sources.credentialReplace', '留空则不改，输入新值覆盖') : t('sources.youtubePlaceholder', 'Netscape 格式的 cookies.txt 内容（scripts/submit_firefox_cookies.py 可以直接提交）')}
                  onChange={(event) => setDrafts((previous) => ({ ...previous, [source]: event.target.value }))}
                />
              ) : (
                <input
                  className="input"
                  id={`source-credential-${source}`}
                  type="password"
                  autoComplete="off"
                  value={drafts[source] || ''}
                  placeholder={configured ? t('sources.credentialReplace', '留空则不改，输入新值覆盖') : source === 'caixin' ? 'UID=…; SA_USER_auth=…' : 'SESSDATA=…'}
                  onChange={(event) => setDrafts((previous) => ({ ...previous, [source]: event.target.value }))}
                />
              )}
            </label>
            <div className="toolbar">
              <button
                className="btn"
                id={`source-credential-save-${source}`}
                type="button"
                disabled={busy === source || !(drafts[source] || '').trim()}
                onClick={() => void save(source)}
              >
                {busy === source ? t('common.saving', '保存中…') : t('common.save', '保存')}
              </button>
              <button
                className="btn ghost"
                id={`source-credential-test-${source}`}
                type="button"
                disabled={busy === source}
                onClick={() => void probe(source)}
              >
                {t('sources.credentialTest', '测试')}
              </button>
              {configured ? (
                <button
                  className="btn ghost"
                  id={`source-credential-clear-${source}`}
                  type="button"
                  disabled={busy === source}
                  onClick={() => void clear(source)}
                >
                  {t('sources.credentialClear', '清除')}
                </button>
              ) : null}
            </div>
            {answer ? (
              <p className={answer.ok ? 'credential-verdict is-ok' : 'credential-verdict is-bad'}>
                {answer.ok ? t('sources.credentialOk', '可用') : t('sources.credentialBad', '不可用')}
                {answer.account ? `（${answer.account}）` : ''} — {answer.detail}
              </p>
            ) : null}
          </div>
        );
      })}

      <p className="muted admin-note">
        {t('sources.credentialNote', '解析、订阅和同步会优先使用个人凭证，实例凭证用于补充。')}
      </p>
    </div>
  );
}
