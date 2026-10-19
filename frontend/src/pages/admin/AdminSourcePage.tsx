import { useCallback, useEffect, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { useI18n } from '../../i18n';
import { useSyncRun } from '../../hooks/useSyncRun';
import { formatRelativeTime } from '../../utils/format';
import { getSourceLabel, getSourceTone } from '../../utils/source';
import { loadSourceOverview, pendingTotal, type SourceOverview, type SourceOverviewPayload } from './sources';

/**
 * Every source in one list: what it is following, whether its credential is in
 * place and what is queued. Each row links to the section that configures it.
 *
 * The counts are the instance's catalogue, not the signed-in admin's own
 * subscriptions: this page answers "what is hippo crawling".
 */
export function AdminSourcePage() {
  const { t } = useI18n();
  const { runSync } = useSyncRun();
  const [payload, setPayload] = useState<SourceOverviewPayload | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState('');
  // A retry races the request it replaces: only the newest answer is applied.
  const requestIdRef = useRef(0);

  const load = useCallback(async () => {
    const requestId = requestIdRef.current + 1;
    requestIdRef.current = requestId;
    setLoading(true);
    setError('');
    try {
      const next = await loadSourceOverview();
      if (requestId !== requestIdRef.current) return;
      setPayload(next);
    } catch (err) {
      if (requestId !== requestIdRef.current) return;
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      if (requestId === requestIdRef.current) setLoading(false);
    }
  }, []);

  useEffect(() => {
    queueMicrotask(() => {
      void load();
    });
  }, [load]);

  const sync = async (source: string) => {
    setBusy(source);
    try {
      // The console runs the instance's own catalogue, not one reader's list.
      await runSync({ source, siteWide: true });
    } finally {
      setBusy('');
    }
  };

  if (loading) {
    return <div className="muted admin-empty">{t('admin.loading', '加载中…')}</div>;
  }

  if (error) {
    return (
      <div className="admin-page">
        <section className="panel">
          <p className="muted admin-empty">{t('admin.source.loadFailed', '加载来源失败。')}</p>
          <div className="admin-actions">
            <button className="btn" type="button" onClick={() => void load()}>
              {t('common.retry', '重试')}
            </button>
          </div>
        </section>
      </div>
    );
  }

  return (
    <div className="admin-page">
      <section className="panel">
        <div className="panel-header">
          <div>
            <h2 className="panel-title">{t('admin.source.title', '来源')}</h2>
            <p className="muted">
              {t('admin.source.subtitle', '每个来源的订阅、凭证与同步状态；点进去改配置。')}
            </p>
          </div>
        </div>

        <div className="admin-table-wrap">
          <table className="admin-table">
            <thead>
              <tr>
                <th>{t('admin.source.name', '来源')}</th>
                <th>{t('admin.source.channels', '订阅')}</th>
                <th>{t('admin.source.credential', '凭证')}</th>
                <th>{t('admin.source.lastSync', '最近同步')}</th>
                <th>{t('admin.source.backlog', '积压')}</th>
                <th>{t('admin.source.actions', '操作')}</th>
              </tr>
            </thead>
            <tbody>
              {(payload?.sources || []).map((row) => (
                <SourceRow
                  key={row.source}
                  row={row}
                  busy={busy === row.source}
                  onSync={() => void sync(row.source)}
                />
              ))}
            </tbody>
          </table>
        </div>

        {!payload?.media_enabled ? (
          <p className="muted admin-note">
            {t('admin.source.mediaDisabled', '媒体来源与转写当前是关闭的；只有公众号会同步。')}
          </p>
        ) : null}
      </section>
    </div>
  );
}

function SourceRow({ row, busy, onSync }: { row: SourceOverview; busy: boolean; onSync: () => void }) {
  const { t } = useI18n();
  const lastSync = row.last_synced_at
    ? formatRelativeTime(row.last_synced_at, t) || t('admin.source.never', '从未')
    : t('admin.source.never', '从未');
  const pending = pendingTotal(row.backlog);
  const failed = row.backlog?.failed ?? 0;

  return (
    <tr>
      <td>
        <span className={`view-rail-dot item-show-type-${getSourceTone(row.source)}`} aria-hidden="true" />
        <Link to={`/admin/source/${row.source}`} className="admin-source-link">
          {getSourceLabel(row.source, t)}
        </Link>
      </td>
      <td>
        {t('admin.source.channelCount', '{n} 个频道').replace('{n}', String(row.channels))}
        {row.followers > 0 ? (
          <span className="muted"> · {t('admin.source.followerCount', '{n} 人订阅').replace('{n}', String(row.followers))}</span>
        ) : null}
      </td>
      <td>
        {row.credential === null ? (
          <span className="muted">{t('admin.source.credentialNotNeeded', '不需要')}</span>
        ) : row.credential.configured ? (
          <span className="admin-badge">{t('admin.source.credentialSet', '已配置')}</span>
        ) : (
          <span className="admin-badge admin-badge-warn">{t('admin.source.credentialMissing', '未配置')}</span>
        )}
      </td>
      <td className="muted">{lastSync}</td>
      <td>
        {pending === 0 && failed === 0 ? (
          <span className="muted">{t('admin.source.backlogEmpty', '无')}</span>
        ) : (
          <span>
            {t('admin.source.backlogPending', '{n} 待处理').replace('{n}', String(pending))}
            {failed > 0 ? (
              <span className="admin-badge admin-badge-warn">
                {t('admin.source.backlogFailed', '{n} 失败').replace('{n}', String(failed))}
              </span>
            ) : null}
          </span>
        )}
      </td>
      <td className="admin-actions">
        {/* 能不能同步取决于有没有人订阅，不是目录里有多少条：一个没人订阅的
            频道点下去只会得到 404。 */}
        <button className="btn ghost" type="button" disabled={busy || row.followers === 0} onClick={onSync}>
          {busy ? t('admin.source.syncing', '排队中…') : t('sync.run', '运行')}
        </button>
        <Link className="btn ghost" to={`/admin/source/${row.source}`}>
          {t('admin.source.configure', '配置')}
        </Link>
      </td>
    </tr>
  );
}
