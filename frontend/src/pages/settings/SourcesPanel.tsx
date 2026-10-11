import { useCallback, useEffect, useState } from 'react';
import { apiGet, apiSend, isAuthError } from '../../api';
import { useI18n } from '../../i18n';
import { useToast } from '../../hooks/useToast';
import { getSourceLabel } from '../../utils/source';

type CredentialState = Record<string, { configured: boolean }>;
type Probe = { ok: boolean; detail: string; account: string; credential: string };

/** The sources hippo can log in for. The script fills YouTube in; this page shows it. */
const SOURCES = ['bilibili', 'youtube'];

/**
 * Per-user source credentials.
 *
 * Bilibili's listing endpoint is risk-controlled and YouTube asks a logged-in
 * session to prove it is not a bot, so both want a cookie. The instance has its
 * own (the admin sets those); this is for a reader who wants to use their own,
 * which is what the operations they trigger by hand use.
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
              'B站投稿列表需要 SESSDATA，YouTube 需要登录态。这里配置自己的；管理台配的是全实例通用的那一份。',
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
                  placeholder={configured ? t('sources.credentialReplace', '留空则不改，输入新值覆盖') : 'SESSDATA=…'}
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
        {t('sources.credentialNote', '凭证只用于你自己触发的操作（解析、订阅）；后台同步用实例级的那一份。')}
      </p>
    </div>
  );
}
