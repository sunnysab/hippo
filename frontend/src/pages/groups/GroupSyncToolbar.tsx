import { useEffect } from 'react';
import { useGroupsState } from '../../store/groups';
import { useI18n } from '../../i18n';
import { apiSend } from '../../api';
import { useToast } from '../../hooks/useToast';
import { syncDefaults } from '../../store/shared';

export function GroupSyncToolbar() {
  const { state } = useGroupsState();
  const { t } = useI18n();
  const { showToast } = useToast();

  const group = state.groups.find((g) => g.id === state.selectedGroupId);

  const defaultMode = syncDefaults.mode;
  const getSyncModeLabel = (mode: string) => {
    if (mode === 'incremental') return t('sync.modeIncremental', 'Incremental');
    if (mode === 'recent') return t('sync.modeRecent', 'Recent');
    if (mode === 'full') return t('sync.modeFull', 'Full');
    return mode;
  };

  useEffect(() => {
    if (!group) return;
    const daysInput = document.getElementById('group-sync-days') as HTMLInputElement | null;
    if (daysInput) {
      daysInput.disabled = group.sync_mode !== 'recent';
    }
  }, [group]);

  if (!group) return null;

  const handleModeChange = async (e: React.ChangeEvent<HTMLSelectElement>) => {
    const mode = e.target.value;
    const daysInput = document.getElementById('group-sync-days') as HTMLInputElement | null;
    if (daysInput) {
      daysInput.disabled = mode !== 'recent';
    }
  };

  const handleSave = async (e: React.ChangeEvent<HTMLSelectElement> | React.ChangeEvent<HTMLInputElement>) => {
    const modeSelect = document.getElementById('group-sync-mode') as HTMLSelectElement | null;
    const daysInput = document.getElementById('group-sync-days') as HTMLInputElement | null;
    const mode = modeSelect?.value || '';
    const days = parseInt(daysInput?.value || String(syncDefaults.recent_days), 10);
    try {
      const body: Record<string, unknown> = { sync_mode: mode || null };
      if (mode === 'recent') {
        body.sync_recent_days = Number.isFinite(days) && days > 0 ? days : syncDefaults.recent_days;
      }
      await apiSend(`/api/group/${group.id}`, 'PATCH', body);
      showToast(t('groups.syncSaved', 'Default sync updated.'));
    } catch {
      showToast(t('groups.syncFailed', 'Failed to update default sync.'));
    }
  };

  const inheritLabel = t('groups.syncModeInherit', 'Follow global ({mode})').replace('{mode}', getSyncModeLabel(defaultMode));

  return (
    <div className="toolbar group-sync-toolbar">
      <span className="muted">{t('groups.syncDefault', 'Default sync')}</span>
      <select
        id="group-sync-mode"
        value={group.sync_mode || ''}
        onChange={(e) => { handleModeChange(e); void handleSave(e); }}
      >
        <option value="">{inheritLabel}</option>
        <option value="incremental">{t('sync.modeIncremental', 'Incremental')}</option>
        <option value="recent">{t('sync.modeRecent', 'Recent')}</option>
        <option value="full">{t('sync.modeFull', 'Full')}</option>
      </select>
      <input
        type="number"
        id="group-sync-days"
        min="1"
        defaultValue={group.sync_recent_days ?? syncDefaults.recent_days}
        disabled={group.sync_mode !== 'recent'}
        onChange={(e) => { void handleSave(e); }}
      />
    </div>
  );
}
