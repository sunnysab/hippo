import { useCallback, useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { useI18n } from '../../i18n';
import { formatRelativeTime } from '../../utils/format';
import { getSourceLabel, getSourceTone } from '../../utils/source';
import { loadSourceOverview, pendingTotal, type SourceOverview } from './sources';

/**
 * One row per source: what is queued and when it last ran.
 *
 * This is the page's index — the reader answers "which source is behind" before
 * scrolling into the global schedule, then follows the row that looks wrong.
 */
export function SourceSyncOverview() {
  const { t } = useI18n();
  const [sources, setSources] = useState<SourceOverview[]>([]);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    try {
      const payload = await loadSourceOverview();
      setSources(payload.sources);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  return (
    <div className="panel">
      <div className="panel-header">
        <div>
          <h2>{t('admin.sync.overviewTitle', '各来源概览')}</h2>
          <p className="muted">{t('admin.sync.overviewSubtitle', '每个来源的积压与最近一次同步。')}</p>
        </div>
        <Link className="btn ghost" to="/admin/source">
          {t('admin.source.title', '来源')}
        </Link>
      </div>

      {loading ? (
        <div className="muted admin-empty">{t('admin.loading', '加载中…')}</div>
      ) : (
        <div className="sync-progress-list">
          {sources.map((row) => {
            const pending = pendingTotal(row.backlog);
            const tone = pending > 0 ? 'info' : 'muted';
            return (
              <Link
                key={row.source}
                to={`/admin/source/${row.source}`}
                className={`sync-progress-item sync-tone-${tone}`}
              >
                <div className="sync-progress-copy">
                  <div className="sync-progress-title-row">
                    <span className={`view-rail-dot item-show-type-${getSourceTone(row.source)}`} aria-hidden="true" />
                    <div className="account-name">{getSourceLabel(row.source, t)}</div>
                    <span className="meta-count">
                      {t('admin.sync.pendingCount', '待处理 {n}').replace('{n}', String(pending))}
                    </span>
                  </div>
                  <div className="account-sub">
                    {formatRelativeTime(row.last_synced_at, t) || t('admin.source.never', '从未')}
                    {' · '}
                    {t('admin.source.channelCount', '{n} 个频道').replace('{n}', String(row.channels))}
                  </div>
                </div>
              </Link>
            );
          })}
        </div>
      )}
    </div>
  );
}
