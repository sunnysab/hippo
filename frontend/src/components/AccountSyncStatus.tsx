import { useState } from 'react';
import type { AccountSync } from '../store/shared';
import { useI18n } from '../i18n';
import { apiSend, isAuthError } from '../api';
import { useToast } from '../hooks/useToast';
import { emitRefresh } from '../utils/events';

const HISTORY_LABELS: Record<string, string> = {
  pending: '等待补全历史列表', queued: '历史列表补全排队中', running: '历史列表补全中',
  cancelling: '正在暂停历史补全', complete: '历史列表已补全', paused: '历史补全已暂停', failed: '历史列表补全失败',
};

export function AccountSyncStatus({ sync, biz, disabled }: { sync: AccountSync; biz: string; disabled: boolean }) {
  const { t } = useI18n();
  const { showToast } = useToast();
  const [busy, setBusy] = useState(false);
  const resume = async () => {
    if (busy) return;
    setBusy(true);
    try {
      await apiSend(`/api/account/${encodeURIComponent(biz)}/backfill`, 'POST', {});
      emitRefresh();
    } catch (err) {
      if (!isAuthError(err)) showToast((err as Error).message || t('sync.resumeFailed', '无法恢复历史补全'));
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="account-sub account-sync-status" aria-live="polite">
      {sync.history.status !== 'unsupported' && (
        <div>{t(`sync.history.${sync.history.status}`, HISTORY_LABELS[sync.history.status] || sync.history.status)}</div>
      )}
      {sync.listing && sync.listing.kind !== 'backfill' && (
        <div>{t(`sync.listing.${sync.listing.status}`, sync.listing.status === 'running' ? '正在检查近期文章' : '近期文章检查排队中')}</div>
      )}
      {(['pending', 'processing', 'failed'] as const).map((status) => sync.articles[status] > 0 && (
        <div key={status}>{t(`sync.bodies.${status}`, '{n}').replace('{n}', String(sync.articles[status]))}</div>
      ))}
      {sync.history.error && <div>{sync.history.error}</div>}
      {!disabled && ['failed', 'paused'].includes(sync.history.status) && (
        <button className="btn ghost" type="button" disabled={busy} onClick={() => void resume()}>
          {t('sync.resumeHistory', '继续补全历史')}
        </button>
      )}
    </div>
  );
}
