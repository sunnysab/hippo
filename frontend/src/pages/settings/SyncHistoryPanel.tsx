import { useSettingsState } from '../../store/settings';
import { useI18n } from '../../i18n';
import { escapeHtml } from '../../utils/format';
import { getSyncTone } from '../../utils/sync';

const HISTORY_PAGE_SIZE = 5;

export function SyncHistoryPanel() {
  const { state } = useSettingsState();
  const { t } = useI18n();

  const history = (state.syncStatus?.history || []).slice(0, HISTORY_PAGE_SIZE);

  if (!history.length) {
    return (
      <div className="panel sync-history">
        <div className="panel-header">
          <div>
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
          <h2>{t('sync.historyTitle', 'Sync History')}</h2>
          <p className="muted">{t('sync.historySubtitle', 'Recent runs and status.')}</p>
        </div>
      </div>
      <div className="list" id="sync-history">
        {history.map((entry: Record<string, unknown>, i: number) => {
          const status = (entry.status as string) || 'unknown';
          const toneClass = `sync-tone-${getSyncTone(status)}`;
          const saved = Number(entry.saved || 0);
          const started = entry.started_at
            ? new Date(entry.started_at as string).toLocaleString('zh-CN')
            : '-';
          const error = entry.error as string | undefined;
          const label = saved > 0 ? `+${saved} 篇` : started;
          return (
            <div key={i} className={`list-item sync-history-item ${toneClass}`}>
              <div className="sync-history-copy">
                <div className="sync-history-top">
                  <div className="account-name">{escapeHtml(label)}</div>
                  <span className={`sync-status-badge ${toneClass}`}>
                    {t(`sync.status.${status}`, '未知')}
                  </span>
                </div>
                <div className="account-sub">{escapeHtml(started)}</div>
                {error && <div className="account-sub">{escapeHtml(error)}</div>}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
