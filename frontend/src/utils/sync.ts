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
