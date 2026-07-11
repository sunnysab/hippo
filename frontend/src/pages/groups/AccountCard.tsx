import { memo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useGroupsState } from '../../store/groups';
import { useI18n } from '../../i18n';
import { apiSend, isAuthError } from '../../api';
import { useToast } from '../../hooks/useToast';
import { formatRelativeTime } from '../../utils/format';
import { syncDefaults } from '../../store/shared';
import { getSyncModeLabel } from '../../utils/sync';
import type { Account } from '../../store/shared';

interface AccountCardProps {
  account: Account;
}

export const AccountCard = memo(function AccountCard({ account }: AccountCardProps) {
  const { state, dispatch } = useGroupsState();
  const { t } = useI18n();
  const { showToast } = useToast();
  const navigate = useNavigate();
  const [syncMode, setSyncMode] = useState(account.sync_mode || '');
  const [syncDays, setSyncDays] = useState(String(account.sync_recent_days ?? syncDefaults.recent_days));
  const [syncInterval, setSyncInterval] = useState<number | null>(account.sync_interval_days ?? null);

  const activeGroup = state.groups.find((g) => g.id === state.selectedGroupId);
  const groupMode = activeGroup?.sync_mode || '';
  const defaultMode = groupMode || syncDefaults.mode;
  const inheritLabel = groupMode
    ? t('accounts.syncModeInheritGroup', 'Follow group ({mode})').replace('{mode}', getSyncModeLabel(t, defaultMode))
    : t('accounts.syncModeInherit', 'Follow global ({mode})').replace('{mode}', getSyncModeLabel(t, defaultMode));

  const groupRecentDays = activeGroup?.sync_recent_days;
  const baseRecentDays = groupRecentDays ?? syncDefaults.recent_days;

  const handleCheck = () => {
    dispatch({ type: 'TOGGLE_SELECTED', biz: account.biz });
  };

  const handleToggleDisabled = async () => {
    const nextDisabled = !account.is_disabled;
    try {
      await apiSend(`/api/account/${account.biz}`, 'PATCH', { is_disabled: nextDisabled });
      dispatch({ type: 'UPDATE_ACCOUNT', biz: account.biz, patch: { is_disabled: nextDisabled } });
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

  const saveSyncSettings = async (nextMode: string, nextDays: string, nextInterval: number | null = syncInterval) => {
    const parsedDays = parseInt(nextDays || String(baseRecentDays), 10);
    const safeDays = Number.isFinite(parsedDays) && parsedDays > 0 ? parsedDays : baseRecentDays;
    const body: Record<string, unknown> = { sync_mode: nextMode || null, sync_interval_days: nextInterval };
    if (nextMode === 'recent') {
      body.sync_recent_days = safeDays;
    }
    try {
      await apiSend(`/api/account/${account.biz}`, 'PATCH', body);
      dispatch({
        type: 'UPDATE_ACCOUNT',
        biz: account.biz,
        patch: {
          sync_mode: nextMode || null,
          sync_recent_days: nextMode === 'recent' ? safeDays : null,
          sync_interval_days: nextInterval,
        },
      });
      showToast(t('accounts.syncSaved', 'Sync strategy updated.'));
    } catch (err) {
      if (isAuthError(err)) return;
      showToast(t('accounts.syncFailed', 'Failed to update sync strategy.'));
    }
  };

  const lastSynced = formatRelativeTime(account.last_synced_at, t);
  const lastUpdatedText = lastSynced
    ? t('accounts.lastUpdated', 'Last update {time}').replace('{time}', lastSynced)
    : t('accounts.lastUpdatedEmpty', 'No updates yet');
  const articleCountText = t('accounts.articleCount', '{n} articles').replace('{n}', String(account.article_count));

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
        <img className="account-avatar" src={account.avatar_url} alt="" onError={(e) => { (e.target as HTMLImageElement).style.display = 'none'; }} />
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
          <div className="account-sub">{account.alias || account.biz}</div>
          <div className="account-sub account-stats">
            <span className="account-stat">{lastUpdatedText}</span>
            <span className="account-stat">{articleCountText}</span>
            {account.is_disabled && (
              <span className="account-tag">{t('accounts.syncDisabled', 'Sync disabled')}</span>
            )}
          </div>
        </div>
      </div>
      <div className="account-sync">
        <div className="account-sync-row">
          <span className="account-sync-label">{t('accounts.syncMode', 'Update strategy')}</span>
          <select
            className="account-sync-mode"
            data-biz={account.biz}
            value={syncMode}
            onChange={(event) => {
              const nextMode = event.target.value;
              setSyncMode(nextMode);
              if (nextMode !== 'recent') {
                setSyncDays(String(account.sync_recent_days ?? baseRecentDays));
              }
              void saveSyncSettings(nextMode, syncDays);
            }}
          >
            <option value="">{inheritLabel}</option>
            <option value="incremental">{t('sync.modeIncremental', 'Incremental')}</option>
            <option value="recent">{t('sync.modeRecent', 'Recent')}</option>
            <option value="full">{t('sync.modeFull', 'Full')}</option>
          </select>
        </div>
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
              void saveSyncSettings(syncMode, syncDays, nextInterval);
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
        <div className={`account-sync-row account-sync-if-recent${syncMode === 'recent' ? ' is-visible' : ''}`}>
          <span className="account-sync-label">{t('accounts.syncRecentDays', 'Recent days')}</span>
          <input
            className="account-sync-days"
            type="number"
            min="1"
            value={syncDays}
            disabled={syncMode !== 'recent'}
            onChange={(event) => setSyncDays(event.target.value)}
            onBlur={() => { void saveSyncSettings(syncMode, syncDays); }}
          />
        </div>
      </div>
    </div>
  );
});
