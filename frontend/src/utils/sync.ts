type Translate = (key: string, fallback: string) => string;

export const getSyncModeLabel = (t: Translate, mode: string): string => {
  if (mode === 'incremental') return t('sync.modeIncremental', 'Incremental');
  if (mode === 'recent') return t('sync.modeRecent', 'Recent');
  if (mode === 'full') return t('sync.modeFull', 'Full');
  return mode;
};

export const getSyncTone = (status: string): string => {
  if (status === 'success' || status === 'completed') return 'success';
  if (status === 'error' || status === 'failed' || status === 'login_required' || status === 'stopped') {
    return 'danger';
  }
  if (status === 'running' || ['starting', 'waiting', 'scanned', 'refresh'].includes(status)) {
    return 'info';
  }
  if (status === 'pending') return 'warning';
  if (status === 'cancelling' || status === 'cancelled') return 'warning';
  if (status === 'skipped') return 'muted';
  return 'neutral';
};
