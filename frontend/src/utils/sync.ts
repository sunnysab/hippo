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
  if (status === 'pending') return 'warning';
  if (status === 'cancelling' || status === 'cancelled') return 'warning';
  if (status === 'skipped') return 'muted';
  return 'neutral';
};

/** What a status reads as while a page is following its own run. */
const SYNC_RUN_LABELS: Record<string, { key: string; fallback: string }> = {
  queued: { key: 'sync.status.pending', fallback: 'Queued' },
  pending: { key: 'sync.status.pending', fallback: 'Queued' },
  running: { key: 'sync.status.running', fallback: 'Updating…' },
  cancelling: { key: 'sync.status.running', fallback: 'Updating…' },
  success: { key: 'sync.status.success', fallback: 'Done' },
  finished: { key: 'sync.status.success', fallback: 'Done' },
  failed: { key: 'sync.status.failed', fallback: 'Failed' },
  cancelled: { key: 'sync.status.cancelled', fallback: 'Cancelled' },
};

export const getSyncRunLabel = (
  status: string | null | undefined,
  t: (key: string, fallback?: string) => string,
): string => {
  const label = SYNC_RUN_LABELS[String(status || '')] || {
    key: 'sync.status.running',
    fallback: 'Updating…',
  };
  return t(label.key, label.fallback);
};
