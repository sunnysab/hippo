import { useCallback, useEffect, useState } from 'react';
import { apiGet, apiSend } from '../../api';
import { useI18n } from '../../i18n';
import { useToast } from '../../hooks/useToast';
import { getSourceLabel } from '../../utils/source';

interface Probe {
  ok: boolean;
  detail: string;
  account: string;
}

/** The sources whose credential hippo actually reads. */
const CREDENTIAL_SOURCES = ['bilibili', 'youtube'];

/**
 * The instance-wide credential for one source.
 *
 * Not merged with the personal one on the settings page, and deliberately so:
 * this is the login the background sync falls back to when none of the account's
 * followers supplied their own, and an admin rotating it must not look like they
 * are editing somebody's session. The precedence lives in ``sources/chain.py``.
 */
export function SourceCredentialPanel({ source }: { source: string }) {
  const { t } = useI18n();
  const { showToast } = useToast();
  const [configured, setConfigured] = useState(false);
  const [loading, setLoading] = useState(true);
  const [draft, setDraft] = useState('');
  const [probe, setProbe] = useState<Probe | null>(null);
  const [busy, setBusy] = useState(false);

  const field = source === 'youtube' ? 'youtube_cookies' : 'bilibili_cookie';

  const load = useCallback(async () => {
    try {
      const payload = await apiGet('/api/admin/media');
      setConfigured(Boolean(payload[`${field}_set`]));
    } finally {
      setLoading(false);
    }
  }, [field]);

  useEffect(() => {
    void load();
  }, [load]);

  const save = async () => {
    if (!draft.trim()) return;
    setBusy(true);
    try {
      await apiSend('/api/admin/media', 'PATCH', { [field]: draft });
      setDraft('');
      await load();
      showToast(t('admin.source.credentialSaved', '已保存。'));
    } catch (error) {
      showToast(error instanceof Error ? error.message : String(error));
    } finally {
      setBusy(false);
    }
  };

  const clear = async () => {
    setBusy(true);
    try {
      await apiSend('/api/admin/media', 'PATCH', { [field]: '' });
      await load();
      showToast(t('admin.source.credentialCleared', '已清除。'));
    } catch (error) {
      showToast(error instanceof Error ? error.message : String(error));
    } finally {
      setBusy(false);
    }
  };

  const test = async () => {
    setBusy(true);
    try {
      const answer = await apiSend(
        `/api/source/credential/${source}/test?scope=instance`,
        'POST',
        {},
      ) as unknown as Probe;
      setProbe(answer);
    } catch (error) {
      showToast(error instanceof Error ? error.message : String(error));
    } finally {
      setBusy(false);
    }
  };

  if (!CREDENTIAL_SOURCES.includes(source)) {
    return (
      <div className="panel">
        <div className="panel-header">
          <div>
            <h2>{t('admin.source.credentialTitle', '凭证')}</h2>
            <p className="muted">
              {t('admin.source.credentialNotNeededLong', '{source} 不需要凭证，匿名即可抓取。').replace(
                '{source}',
                getSourceLabel(source, t),
              )}
            </p>
          </div>
        </div>
      </div>
    );
  }

  if (loading) {
    return <div className="panel muted">{t('admin.loading', '加载中…')}</div>;
  }

  return (
    <div className="panel">
      <div className="panel-header">
        <div>
          <h2>{t('admin.source.credentialTitle', '凭证')}</h2>
          <p className="muted">
            {t(
              'admin.source.credentialInstanceHint',
              '实例凭证：同步时先试订阅者自己的，都没有才用它。',
            )}
          </p>
        </div>
      </div>

      <label className="auth-field">
        <span>
          {t('admin.source.credentialInstance', '实例凭证')} ·{' '}
          {configured ? t('sources.credentialSet', '已配置') : t('sources.credentialUnset', '未配置')}
        </span>
        {source === 'youtube' ? (
          <textarea
            className="input"
            rows={4}
            spellCheck={false}
            value={draft}
            placeholder={t('admin.media.youtubeCookiesPlaceholder', '留空则不使用登录态；提交后不会回显')}
            onChange={(event) => setDraft(event.target.value)}
          />
        ) : (
          <input
            className="input"
            type="password"
            autoComplete="off"
            value={draft}
            placeholder="SESSDATA=…"
            onChange={(event) => setDraft(event.target.value)}
          />
        )}
      </label>
      <div className="toolbar">
        <button className="btn" type="button" disabled={busy || !draft.trim()} onClick={() => void save()}>
          {t('common.save', '保存')}
        </button>
        <button className="btn ghost" type="button" disabled={busy} onClick={() => void test()}>
          {t('sources.credentialTest', '测试')}
        </button>
        {configured ? (
          <button className="btn ghost" type="button" disabled={busy} onClick={() => void clear()}>
            {t('sources.credentialClear', '清除')}
          </button>
        ) : null}
      </div>

      {probe ? (
        <p className={probe.ok ? 'credential-verdict is-ok' : 'credential-verdict is-bad'}>
          {probe.ok ? t('sources.credentialOk', '可用') : t('sources.credentialBad', '不可用')}
          {probe.account ? `（${probe.account}）` : ''} — {probe.detail}
        </p>
      ) : null}

      <p className="muted admin-note">
        {t(
          'admin.source.credentialPersonalNote',
          '订阅者自己的凭证在「设置 → 来源凭证」里各自维护，会优先使用。',
        )}
      </p>
    </div>
  );
}
