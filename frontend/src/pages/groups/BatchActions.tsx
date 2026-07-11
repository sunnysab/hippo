import { useState } from 'react';
import { useGroupsState } from '../../store/groups';
import { useI18n } from '../../i18n';
import { apiSend, isAuthError } from '../../api';
import { useToast } from '../../hooks/useToast';
import { useMediaQuery } from '../../hooks/useMediaQuery';
import { ConfirmModal } from '../../components/ConfirmModal';
import { syncDefaults } from '../../store/shared';
import { getSyncModeLabel } from '../../utils/sync';
import { emitRefresh } from '../../utils/events';

export function BatchActions() {
  const { state, dispatch } = useGroupsState();
  const { t } = useI18n();
  const { showToast } = useToast();
  const [expanded, setExpanded] = useState(false);
  const [targetGroupId, setTargetGroupId] = useState('');
  const [syncMode, setSyncMode] = useState('');
  const [syncDays, setSyncDays] = useState(String(syncDefaults.recent_days));
  const [syncBatchInterval, setSyncBatchInterval] = useState<number | null>(null);
  const [showDeleteModal, setShowDeleteModal] = useState(false);
  const isNarrow = useMediaQuery('(max-width: 720px)');

  const count = state.selectedAccounts.length;
  const showActions = isNarrow ? count > 0 && expanded : count > 0;

  const handleSelectAll = (e: React.ChangeEvent<HTMLInputElement>) => {
    if (e.target.checked) {
      dispatch({ type: 'SET_SELECTED_ALL', bizList: state.accounts.map((a) => a.biz) });
    } else {
      dispatch({ type: 'CLEAR_SELECTED' });
    }
  };

  const activeGroup = state.groups.find((g) => g.id === state.selectedGroupId);
  const groupMode = activeGroup?.sync_mode || '';
  const defaultMode = groupMode || syncDefaults.mode;
  const inheritLabel = groupMode
    ? t('accounts.syncModeInheritGroup', 'Follow group ({mode})').replace('{mode}', getSyncModeLabel(t, defaultMode))
    : t('accounts.syncModeInherit', 'Follow global ({mode})').replace('{mode}', getSyncModeLabel(t, defaultMode));

  const handleMove = async () => {
    if (!targetGroupId) {
      showToast(t('accounts.moveSelectGroup', 'Select a target group.'));
      return;
    }
    if (!count) {
      showToast(t('accounts.moveSelectAccounts', 'Select accounts to move.'));
      return;
    }
    try {
      await apiSend('/api/account/move', 'POST', {
        group_id: Number(targetGroupId),
        biz_list: state.selectedAccounts,
      });
      dispatch({ type: 'CLEAR_SELECTED' });
      setTargetGroupId('');
      emitRefresh();
    } catch (err) {
      if (isAuthError(err)) return;
      showToast(t('accounts.moveFailed', 'Failed to move selected accounts.'));
    }
  };

  const handleBatchSyncSettings = async () => {
    if (!count) {
      showToast(t('accounts.syncSelectAccounts', 'Select accounts to sync.'));
      return;
    }
    const body: Record<string, unknown> = {
      biz_list: state.selectedAccounts,
      sync_mode: syncMode || null,
      sync_interval_days: syncBatchInterval,
    };
    if (syncMode === 'recent') {
      const days = parseInt(syncDays || String(syncDefaults.recent_days), 10);
      body.sync_recent_days = Number.isFinite(days) && days > 0 ? days : syncDefaults.recent_days;
    }
    try {
      await apiSend('/api/account/batch', 'POST', body);
      showToast(t('accounts.syncSaved', 'Sync strategy updated.'));
      emitRefresh();
    } catch (err) {
      if (isAuthError(err)) return;
      showToast(t('accounts.syncFailed', 'Failed to update sync strategy.'));
    }
  };

  const handleBatchGroupSync = async () => {
    if (!count) {
      showToast(t('accounts.syncSelectAccounts', 'Select accounts to sync.'));
      return;
    }
    try {
      await apiSend('/api/settings/run', 'POST', { biz_list: state.selectedAccounts });
      window.location.hash = '#/settings/sync';
      showToast(t('accounts.syncTriggered', 'Selected accounts sync started.'));
    } catch (err) {
      if (isAuthError(err)) return;
      showToast(t('accounts.syncTriggerFailed', 'Failed to start selected accounts sync.'));
    }
  };

  const handleDelete = async () => {
    const bizList = state.selectedAccounts;
    let failed = 0;
    for (const biz of bizList) {
      try {
        await apiSend(`/api/account/${biz}`, 'DELETE', {});
        dispatch({ type: 'REMOVE_ACCOUNT', biz });
      } catch (err) {
        if (isAuthError(err)) return;
        failed++;
      }
    }
    dispatch({ type: 'CLEAR_SELECTED' });
    if (failed === 0) {
      showToast(t('accounts.deleteSuccess', 'Deleted {n} accounts with articles and images.').replace('{n}', String(bizList.length)));
    } else {
      showToast(t('accounts.deleteFailed', 'Failed to delete {failed}/{total} accounts.').replace('{failed}', String(failed)).replace('{total}', String(bizList.length)));
    }
    emitRefresh();
  };

  const allChecked = state.accounts.length > 0 && count === state.accounts.length;
  const indeterminate = count > 0 && count < state.accounts.length;

  return (
    <>
      <div className="toolbar accounts-toolbar">
      <label className="switch inline">
        <input
          type="checkbox"
          id="account-select-all"
          checked={allChecked}
          ref={(el) => {
            if (el) el.indeterminate = indeterminate;
          }}
          onChange={handleSelectAll}
        />
        <span>{t('accounts.selectAll', 'Select All')}</span>
      </label>
      {isNarrow && (
        <button
          className={`btn ghost accounts-batch-toggle${count > 0 ? '' : ' is-hidden'}`}
          id="btn-batch-toggle"
          type="button"
          onClick={() => setExpanded(!expanded)}
          aria-expanded={expanded}
        >
          {expanded ? t('accounts.batchHide', 'Hide Batch') : t('accounts.batchToggle', 'Batch Actions')}
          {count > 0 ? ` (${count})` : ''}
        </button>
      )}
      <div className={`batch-actions${showActions ? '' : ' is-hidden'}`} id="batch-actions">
        <select
          id="batch-group-select"
          value={targetGroupId}
          onChange={(event) => setTargetGroupId(event.target.value)}
        >
          <option value="">{t('accounts.movePlaceholder', 'Move to group')}</option>
          {state.groups.map((g) => (
            <option key={g.id} value={g.id}>{g.name}</option>
          ))}
        </select>
        <button className="btn ghost" id="btn-batch-move" type="button" onClick={handleMove}>
          {t('accounts.move', 'Move')}
        </button>
        <div className="batch-divider"></div>
        <select
          id="batch-sync-mode"
          value={syncMode}
          onChange={(event) => setSyncMode(event.target.value)}
        >
          <option value="">{inheritLabel}</option>
          <option value="incremental">{t('sync.modeIncremental', 'Incremental')}</option>
          <option value="recent">{t('sync.modeRecent', 'Recent')}</option>
          <option value="full">{t('sync.modeFull', 'Full')}</option>
        </select>
        <input
          type="number"
          id="batch-sync-days"
          min="1"
          value={syncDays}
          disabled={syncMode !== 'recent'}
          onChange={(event) => setSyncDays(event.target.value)}
        />
        <select
          id="batch-sync-interval"
          value={syncBatchInterval === null ? '' : String(syncBatchInterval)}
          onChange={(event) => {
            const val = event.target.value;
            setSyncBatchInterval(val === '' ? null : Number(val));
          }}
        >
          <option value="">{t('accounts.syncIntervalAuto', 'Auto (detect)')}</option>
          <option value="1">{t('accounts.syncInterval1', 'Every run')}</option>
          <option value="3">{t('accounts.syncInterval3', 'Every 3 days')}</option>
          <option value="7">{t('accounts.syncInterval7', 'Weekly')}</option>
          <option value="14">{t('accounts.syncInterval14', 'Biweekly')}</option>
          <option value="30">{t('accounts.syncInterval30', 'Monthly')}</option>
        </select>
        <button className="btn ghost" id="btn-batch-sync" type="button" onClick={handleBatchSyncSettings}>
          {t('accounts.batchSyncApply', 'Apply')}
        </button>>
          {t('accounts.batchSyncApply', 'Apply')}
        </button>
        <div className="batch-divider"></div>
        <button className="btn ghost" id="btn-batch-group-sync" type="button" onClick={handleBatchGroupSync}>
          {t('accounts.groupSync', 'Sync')}
        </button>
        <div className="batch-divider"></div>
        <button className="btn ghost" id="btn-batch-delete" type="button" onClick={() => setShowDeleteModal(true)}>
          {t('accounts.delete', 'Delete')}
        </button>
      </div>
      <span className="meta-count" id="batch-count">{count}</span>
      <button
        className="btn ghost"
        id="btn-account-refresh"
        type="button"
        onClick={emitRefresh}
      >
        <span>{t('actions.refresh', 'Refresh')}</span>
      </button>
    </div>
    <ConfirmModal
      isOpen={showDeleteModal}
      title={t('accounts.deleteConfirmTitle', 'Confirm delete')}
      message={t('accounts.deleteBatchConfirmMessage', 'Delete {count} accounts and all their articles/images. This cannot be undone.')
        .replace('{count}', String(count))}
      confirmLabel={t('accounts.delete', 'Delete')}
      onClose={() => setShowDeleteModal(false)}
      onConfirm={handleDelete}
    />
    </>
  );
}
