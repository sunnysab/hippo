import { useSettingsState } from '../../store/settings';
import { useI18n } from '../../i18n';
import { escapeHtml, formatRelativeTime } from '../../utils/format';
import { getSyncTone } from '../../utils/sync';

const CountRow = ({ label, value, tone }: { label: string; value: number; tone: string }) => (
  <div className={`sync-progress-item ${tone ? `sync-tone-${tone}` : ''}`}>
    <div className="sync-progress-copy">
      <div className="sync-progress-title-row">
        <div className="account-name">{label}</div>
        <span className="meta-count">{value}</span>
      </div>
    </div>
  </div>
);

/**
 * 队列水位：列表任务之外的「正文抓取 / 图片下载」积压。
 *
 * 数字由 worker 定期写进 meta（`sync:queue_stats`），失败样本实时查 `article_queue`，
 * 所以这里看到的是 worker 最后活跃时的状态。
 */
export function QueuePanel() {
  const { state } = useSettingsState();
  const { t } = useI18n();

  const queue = state.syncStatus?.queue;
  const articles = queue?.articles;
  const images = queue?.images;
  const failed = queue?.failed_items || [];
  const heartbeat = state.syncStatus?.worker_heartbeat_at || '';
  const heartbeatText = heartbeat
    ? t('sync.workerAlive', 'worker last seen {time}').replace('{time}', formatRelativeTime(heartbeat, t))
    : t('sync.workerUnknown', 'no worker heartbeat yet');

  return (
    <div className="panel sync-queue">
      <div className="panel-header">
        <div>
          <h2>{t('sync.queueTitle', 'Queue Backlog')}</h2>
          <p className="muted">{t('sync.queueSubtitle', 'Pending article bodies and images.')}</p>
        </div>
        <div className="toolbar">
          <span className="muted" id="worker-heartbeat">{heartbeatText}</span>
        </div>
      </div>
      <div className="sync-active-body" id="sync-queue">
        <div className="sync-progress-list">
          <CountRow
            label={t('sync.queueArticlesPending', 'Article bodies pending')}
            value={articles?.pending ?? 0}
            tone="pending"
          />
          <CountRow
            label={t('sync.queueArticlesProcessing', 'Article bodies in flight')}
            value={articles?.processing ?? 0}
            tone="running"
          />
          <CountRow
            label={t('sync.queueArticlesFailed', 'Article bodies failed')}
            value={articles?.failed ?? 0}
            tone="failed"
          />
          <CountRow
            label={t('sync.queueImagesPending', 'Images pending')}
            value={images?.pending ?? 0}
            tone="pending"
          />
          <CountRow
            label={t('sync.queueImagesFailed', 'Images failed')}
            value={images?.failed ?? 0}
            tone="failed"
          />
        </div>
        {failed.length > 0 && (
          <div className="sync-progress-list">
            {failed.map((item) => (
              <div key={`${item.biz}:${item.sn}`} className={`sync-progress-item sync-tone-${getSyncTone('failed')}`}>
                <div className="sync-progress-copy">
                  <div className="sync-progress-title-row">
                    <div className="account-name">{escapeHtml(item.nickname || item.biz)}</div>
                    <span className="meta-count">{t('sync.queueAttempts', '{n} tries').replace('{n}', String(item.attempts))}</span>
                  </div>
                  <div className="account-sub">{escapeHtml(item.last_error || item.sn)}</div>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
