import { memo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useGroupsState } from '../../store/groups';
import { useI18n } from '../../i18n';
import { apiSend, isAuthError } from '../../api';
import { useToast } from '../../hooks/useToast';
import { formatRelativeTime } from '../../utils/format';
import { emitRefresh } from '../../utils/events';
import { AccountSyncStatus } from '../../components/AccountSyncStatus';
import { hasAccountWork } from '../../utils/sync';
import type { Account } from '../../store/shared';

interface AccountCardProps {
  account: Account;
}

export const AccountCard = memo(function AccountCard({ account }: AccountCardProps) {
  const { state, dispatch } = useGroupsState();
  const { t } = useI18n();
  const { showToast } = useToast();
  const navigate = useNavigate();
  const [syncInterval, setSyncInterval] = useState<number | null>(account.sync_interval_days ?? null);

  const handleCheck = () => {
    dispatch({ type: 'TOGGLE_SELECTED', biz: account.biz });
  };

  const handleToggleDisabled = async () => {
    const nextDisabled = !account.is_disabled;
    try {
      await apiSend(`/api/account/${account.biz}`, 'PATCH', { is_disabled: nextDisabled });
      dispatch({ type: 'UPDATE_ACCOUNT', biz: account.biz, patch: { is_disabled: nextDisabled } });
      emitRefresh();
      showToast(
        nextDisabled
          ? t('accounts.syncDisabledToast', 'Sync disabled.')
          : t('accounts.syncEnabledToast', 'Sync enabled.'),
      );
    } catch (err) {
      if (isAuthError(err)) return;
      showToast(t('accounts.syncToggleFailed', 'Failed to update sync status.'));
    }
  };

  const saveSyncInterval = async (nextInterval: number | null) => {
    try {
      await apiSend(`/api/account/${account.biz}`, 'PATCH', { sync_interval_days: nextInterval });
      dispatch({ type: 'UPDATE_ACCOUNT', biz: account.biz, patch: { sync_interval_days: nextInterval } });
      showToast(t('accounts.syncSaved', 'Sync strategy updated.'));
    } catch (err) {
      if (isAuthError(err)) return;
      showToast(t('accounts.syncFailed', 'Failed to update sync strategy.'));
    }
  };

  const lastSynced = formatRelativeTime(account.last_synced_at, t);
  const lastPublished = formatRelativeTime(account.latest_publish_at, t);
  const lastUpdatedText = lastPublished
    ? t('accounts.lastUpdated', 'Last update {time}').replace('{time}', lastPublished)
    : t('accounts.lastUpdatedEmpty', 'No publication time');
  const lastSyncedText = lastSynced
    ? t('accounts.lastSynced', 'Synced {time}').replace('{time}', lastSynced)
    : t('accounts.lastSyncedEmpty', 'Not synced yet');
  const articleCountText = t('accounts.articleCount', '{n} articles').replace('{n}', String(account.article_count));
  const syncActive = account.sync ? hasAccountWork(account.sync) : account.backfill_running || account.backfill_pending;
  const syncRunning = account.sync
    ? ['running', 'cancelling'].includes(account.sync.history.status)
      || account.sync.listing?.status === 'running' || account.sync.articles.processing > 0
    : account.backfill_running;
  const syncFailed = Boolean(account.sync && (
    account.sync.history.status === 'failed' || account.sync.listing?.status === 'failed'
    || account.sync.articles.failed > 0 || (account.sync.images?.failed || 0) > 0
  ));

  const handleViewArticles = () => {
    const params = new URLSearchParams();
    if (account.group_id) params.set('group', String(account.group_id));
    params.set('account', account.biz);
    navigate(`/articles?${params.toString()}`);
  };

  return (
    <div className={`card${account.is_disabled ? ' is-disabled' : ''}`}>
      <div className="card-header">
        <input
          className="account-check"
          type="checkbox"
          data-biz={account.biz}
          checked={state.selectedAccounts.includes(account.biz)}
          onChange={handleCheck}
        />
        <button
          className="account-toggle"
          type="button"
          data-biz={account.biz}
          title={account.is_disabled ? t('accounts.enableSync', 'Enable sync') : t('accounts.disableSync', 'Disable sync')}
          aria-label={account.is_disabled ? t('accounts.enableSync', 'Enable sync') : t('accounts.disableSync', 'Disable sync')}
          onClick={handleToggleDisabled}
        >
          {account.is_disabled
            ? <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M8 5v14l11-7z"/></svg>
            : <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 5h4v14H6zm8 0h4v14h-4z"/></svg>
          }
        </button>
      </div>
      <div className="account-meta">
        <img className="account-avatar" src={account.avatar_url} alt="" onError={(e) => { (e.target as HTMLImageElement).style.visibility = 'hidden'; }} />
        <div>
          <div
            className="account-name"
            role="button"
            tabIndex={0}
            onClick={handleViewArticles}
            onKeyDown={(event) => {
              if (event.key === 'Enter' || event.key === ' ') {
                event.preventDefault();
                handleViewArticles();
              }
            }}
          >
            {account.nickname}
          </div>
          <div className="account-sub account-stats">
            <span className="account-stat">{lastUpdatedText}</span>
            {!account.is_disabled && syncActive && (
              <span className="account-tag account-tag-live">
                <span className="sync-dot" aria-hidden="true" />
                {syncRunning ? t('accounts.syncingShort', '同步中') : t('accounts.syncQueuedShort', '等待同步')}
              </span>
            )}
            {account.is_disabled && (
              <span className="account-tag">{t('accounts.syncDisabled', 'Sync disabled')}</span>
            )}
            {syncFailed && (
              <span className="account-tag">{t('accounts.syncFailedShort', '同步有失败')}</span>
            )}
          </div>
          <details className="account-details">
            <summary>{t('accounts.syncDetails', '同步详情')}</summary>
            <div className="account-sub">{account.alias || account.biz}</div>
            <div className="account-sub">{articleCountText}</div>
            {(lastSynced || (!account.sync && !account.backfill_running && !account.backfill_pending)) && (
              <div className="account-sub">{lastSyncedText}</div>
            )}
            {account.sync && <AccountSyncStatus sync={account.sync} biz={account.biz} disabled={account.is_disabled} />}
            {!account.sync && (account.backfill_running || account.backfill_pending) && (
              <div className="account-sub">{account.backfill_running
                ? t('accounts.syncing', 'Fetching article history')
                : t('accounts.syncQueued', 'Article history queued')}</div>
            )}
            <div className="account-sync-row">
              <span className="account-sync-label">{t('accounts.syncInterval', 'Sync interval')}</span>
              <select
                className="account-sync-interval"
                data-biz={account.biz}
                value={syncInterval === null ? '' : String(syncInterval)}
                onChange={(event) => {
                  const val = event.target.value;
                  const nextInterval = val === '' ? null : Number(val);
                  setSyncInterval(nextInterval);
                  void saveSyncInterval(nextInterval);
                }}
              >
                <option value="">{t('accounts.syncIntervalAuto', 'Auto (detect)')}</option>
                <option value="1">{t('accounts.syncInterval1', 'Every run')}</option>
                <option value="3">{t('accounts.syncInterval3', 'Every 3 days')}</option>
                <option value="7">{t('accounts.syncInterval7', 'Weekly')}</option>
                <option value="14">{t('accounts.syncInterval14', 'Biweekly')}</option>
                <option value="30">{t('accounts.syncInterval30', 'Monthly')}</option>
              </select>
            </div>
          </details>
        </div>
      </div>
    </div>
  );
});
