import { useSettingsState } from '../../store/settings';
import { useI18n } from '../../i18n';
import { escapeHtml } from '../../utils/format';

const HISTORY_PAGE_SIZE = 5;

export function SyncHistoryPanel() {
  const { state } = useSettingsState();
  const { t } = useI18n();

  const getSyncTone = (status: string) => {
    if (status === 'success' || status === 'completed') return 'success';
    if (status === 'failed' || status === 'login_required' || status === 'stopped') return 'danger';
    if (status === 'running') return 'info';
    if (status === 'pending') return 'warning';
    return 'neutral';
  };

  const history = (state.syncStatus?.history || []).slice(0, HISTORY_PAGE_SIZE);

  if (!history.length) {
    return (
      <div className="panel sync-history">
        <div className="panel-header">
          <div>
            <div className="section-kicker">{t('sync.historyKicker', 'History')}</div>
            <h2>{t('sync.historyTitle', 'Sync History')}</h2>
            <p className="muted">{t('sync.historySubtitle', 'Recent runs and status.')}</p>
          </div>
        </div>
        <div className="list" id="sync-history">
          <div className="empty-state">{t('sync.historyEmpty', 'No sync history.')}</div>
        </div>
      </div>
    );
  }

  return (
    <div className="panel sync-history">
      <div className="panel-header">
        <div>
          <div className="section-kicker">{t('sync.historyKicker', 'History')}</div>
          <h2>{t('sync.historyTitle', 'Sync History')}</h2>
          <p className="muted">{t('sync.historySubtitle', 'Recent runs and status.')}</p>
        </div>
      </div>
      <div className="list" id="sync-history">
        {history.map((entry: Record<string, unknown>, i: number) => {
          const status = (entry.status as string) || 'unknown';
          const toneClass = `sync-tone-${getSyncTone(status)}`;
          const saved = entry.saved || 0;
          const started = entry.started_at
            ? new Date(entry.started_at as string).toLocaleString('zh-CN')
            : '-';
          const error = entry.error as string | undefined;
          return (
            <div key={i} className={`list-item sync-history-item ${toneClass}`}>
              <div className="sync-history-copy">
                <div className="sync-history-top">
                  <div className="account-name">
                    {t(`sync.status.${status}`, status)}
                  </div>
                  <span className={`sync-status-badge ${toneClass}`}>
                    {t(`sync.status.${status}`, status)}
                  </span>
                </div>
                <div className="account-sub">{escapeHtml(started)}</div>
                {error && <div className="account-sub">{escapeHtml(error)}</div>}
              </div>
              <span className="badge">+{escapeHtml(String(saved))}</span>
            </div>
          );
        })}
      </div>
    </div>
  );
}
