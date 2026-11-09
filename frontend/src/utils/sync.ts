import type { AccountSync } from '../store/shared';

export const getSyncTone = (status: string): string => {
  if (status === 'success' || status === 'completed' || status === 'ok' || status === 'online') return 'success';
  if (status === 'error' || status === 'failed' || status === 'stopped') {
    return 'danger';
  }
  if (status === 'expired' || status === 'unreachable') return 'danger';
  if (status === 'missing' || status === 'logged_out') return 'warning';
  if (status === 'running' || ['starting', 'waiting', 'scanned', 'refresh'].includes(status)) {
    return 'info';
  }
  if (status === 'queued' || status === 'pending') return 'warning';
  if (status === 'cancelling' || status === 'cancelled') return 'warning';
  if (status === 'skipped') return 'muted';
  return 'neutral';
};

/** What a status reads as while a page is following its own run. */
const SYNC_RUN_LABELS: Record<string, { key: string; fallback: string }> = {
  queued: { key: 'sync.status.pending', fallback: 'Queued' },
  pending: { key: 'sync.status.pending', fallback: 'Queued' },
  running: { key: 'sync.status.running', fallback: 'Updating…' },
  cancelling: { key: 'sync.cancelling', fallback: 'Cancelling…' },
  success: { key: 'sync.status.success', fallback: 'Done' },
  finished: { key: 'sync.status.success', fallback: 'Done' },
  failed: { key: 'sync.status.failed', fallback: 'Failed' },
  completed: { key: 'sync.status.success', fallback: 'Done' },
  skipped: { key: 'sync.status.skipped', fallback: 'Skipped' },
  stopped: { key: 'sync.status.stopped', fallback: 'Stopped' },
  cancelled: { key: 'sync.status.cancelled', fallback: 'Cancelled' },
};

export const getSyncRunLabel = (
  status: string | null | undefined,
  t: (key: string, fallback?: string) => string,
): string => {
  const label = SYNC_RUN_LABELS[String(status || '')] || {
    key: 'sync.status.unknown',
    fallback: 'Unknown',
  };
  return t(label.key, label.fallback);
};

/** Discovery jobs enqueue bodies; their counts are not stored-article counts. */
export const getSyncTaskKind = (kind: string | undefined, t: (key: string, fallback?: string) => string): string => {
  const labels: Record<string, string> = { backfill: '历史列表补全', scheduled: '定时检查近期文章', manual: '手动检查近期文章' };
  return t(`sync.kind.${kind}`, labels[kind || ''] || '检查近期文章');
};

export function hasAccountWork(sync: AccountSync): boolean {
  return Boolean(sync.listing) || ['pending', 'queued', 'running', 'cancelling'].includes(sync.history.status)
    || sync.articles.pending > 0 || sync.articles.processing > 0 || (sync.images?.pending || 0) > 0;
}
