import { useState } from 'react';
import { useGroupsState } from '../../store/groups';
import { useI18n } from '../../i18n';
import { apiSend } from '../../api';
import { useToast } from '../../hooks/useToast';
import { syncDefaults } from '../../store/shared';

export function BatchActions() {
  const { state, dispatch } = useGroupsState();
  const { t } = useI18n();
  const { showToast } = useToast();
  const [expanded, setExpanded] = useState(false);

  const count = state.selectedAccounts.length;
  const isNarrow = window.matchMedia('(max-width: 720px)').matches;
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
  const getSyncModeLabel = (mode: string) => {
    if (mode === 'incremental') return t('sync.modeIncremental', 'Incremental');
    if (mode === 'recent') return t('sync.modeRecent', 'Recent');
    if (mode === 'full') return t('sync.modeFull', 'Full');
    return mode;
  };
  const inheritLabel = groupMode
    ? t('accounts.syncModeInheritGroup', 'Follow group ({mode})').replace('{mode}', getSyncModeLabel(defaultMode))
    : t('accounts.syncModeInherit', 'Follow global ({mode})').replace('{mode}', getSyncModeLabel(defaultMode));

  const handleMove = async () => {
    const select = document.getElementById('batch-group-select') as HTMLSelectElement | null;
    const groupId = select?.value;
    if (!groupId) {
      alert(t('accounts.moveSelectGroup', 'Select a target group.'));
      return;
    }
    if (!count) {
      alert(t('accounts.moveSelectAccounts', 'Select accounts to move.'));
      return;
    }
    await apiSend('/api/account/move', 'POST', {
      group_id: Number(groupId),
      biz_list: state.selectedAccounts,
    });
    dispatch({ type: 'CLEAR_SELECTED' });
  };

  const handleBatchSyncSettings = async () => {
    if (!count) {
      alert(t('accounts.moveSelectAccounts', 'Select accounts to move.'));
      return;
    }
    const modeSelect = document.getElementById('batch-sync-mode') as HTMLSelectElement | null;
    const daysInput = document.getElementById('batch-sync-days') as HTMLInputElement | null;
    const mode = modeSelect?.value || '';
    const body: Record<string, unknown> = {
      biz_list: state.selectedAccounts,
      sync_mode: mode || null,
    };
    if (mode === 'recent') {
      const days = parseInt(daysInput?.value || String(syncDefaults.recent_days), 10);
      body.sync_recent_days = Number.isFinite(days) && days > 0 ? days : syncDefaults.recent_days;
    }
    try {
      await apiSend('/api/account/batch', 'POST', body);
      showToast(t('accounts.syncSaved', 'Sync strategy updated.'));
    } catch {
      showToast(t('accounts.syncFailed', 'Failed to update sync strategy.'));
    }
  };

  const handleBatchGroupSync = async () => {
    if (!count) {
      alert(t('accounts.moveSelectAccounts', 'Select accounts to move.'));
      return;
    }
    try {
      await apiSend('/api/settings/run', 'POST', { biz_list: state.selectedAccounts });
      window.location.hash = '#/settings';
      showToast(t('accounts.syncTriggered', 'Selected accounts sync started.'));
    } catch {
      showToast(t('accounts.syncTriggerFailed', 'Failed to start selected accounts sync.'));
    }
  };

  const allChecked = state.accounts.length > 0 && count === state.accounts.length;
  const indeterminate = count > 0 && count < state.accounts.length;

  return (
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
        <select id="batch-group-select">
          <option value="">{t('accounts.movePlaceholder', 'Move to group')}</option>
          {state.groups.map((g) => (
            <option key={g.id} value={g.id}>{g.name}</option>
          ))}
        </select>
        <button className="btn ghost" id="btn-batch-move" type="button" onClick={handleMove}>
          {t('accounts.move', 'Move')}
        </button>
        <div className="batch-divider"></div>
        <select id="batch-sync-mode" defaultValue="">
          <option value="">{inheritLabel}</option>
          <option value="incremental">{t('sync.modeIncremental', 'Incremental')}</option>
          <option value="recent">{t('sync.modeRecent', 'Recent')}</option>
          <option value="full">{t('sync.modeFull', 'Full')}</option>
        </select>
        <input type="number" id="batch-sync-days" min="1" defaultValue={syncDefaults.recent_days} />
        <button className="btn ghost" id="btn-batch-sync" type="button" onClick={handleBatchSyncSettings}>
          {t('accounts.batchSyncApply', 'Apply')}
        </button>
        <div className="batch-divider"></div>
        <button className="btn ghost" id="btn-batch-group-sync" type="button" onClick={handleBatchGroupSync}>
          {t('accounts.groupSync', 'Sync')}
        </button>
      </div>
      <span className="badge" id="batch-count">{count}</span>
      <button className="btn ghost" id="btn-account-refresh" type="button">
        <span>{t('actions.refresh', 'Refresh')}</span>
      </button>
    </div>
  );
}
