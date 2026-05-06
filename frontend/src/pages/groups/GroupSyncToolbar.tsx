import { useState } from 'react';
import { useGroupsState } from '../../store/groups';
import { useI18n } from '../../i18n';
import { apiSend, isAuthError } from '../../api';
import { useToast } from '../../hooks/useToast';
import { syncDefaults } from '../../store/shared';
import { getSyncModeLabel } from '../../utils/sync';
import type { Group } from '../../store/shared';

interface GroupSyncToolbarFieldsProps {
  group: Group;
}

function GroupSyncToolbarFields({ group }: GroupSyncToolbarFieldsProps) {
  const { dispatch } = useGroupsState();
  const { t } = useI18n();
  const { showToast } = useToast();
  const [mode, setMode] = useState(group.sync_mode || '');
  const [days, setDays] = useState(String(group.sync_recent_days ?? syncDefaults.recent_days));

  const defaultMode = syncDefaults.mode;

  const handleSave = async (nextMode: string, nextDays: string) => {
    const parsedDays = parseInt(nextDays || String(syncDefaults.recent_days), 10);
    try {
      const body: Record<string, unknown> = { sync_mode: nextMode || null };
      const safeDays = Number.isFinite(parsedDays) && parsedDays > 0 ? parsedDays : syncDefaults.recent_days;
      if (nextMode === 'recent') {
        body.sync_recent_days = safeDays;
      }
      await apiSend(`/api/group/${group.id}`, 'PATCH', body);
      dispatch({
        type: 'UPDATE_GROUP',
        groupId: group.id,
        patch: {
          sync_mode: nextMode || null,
          sync_recent_days: nextMode === 'recent' ? safeDays : null,
        },
      });
      showToast(t('groups.syncSaved', 'Default sync updated.'));
    } catch (err) {
      if (isAuthError(err)) return;
      showToast(t('groups.syncFailed', 'Failed to update default sync.'));
    }
  };

  const inheritLabel = t('groups.syncModeInherit', 'Follow global ({mode})').replace('{mode}', getSyncModeLabel(t, defaultMode));

  return (
    <div className="toolbar group-sync-toolbar">
      <span className="muted">{t('groups.syncDefault', 'Default sync')}</span>
      <select
        id="group-sync-mode"
        value={mode}
        onChange={(event) => {
          const nextMode = event.target.value;
          setMode(nextMode);
          if (nextMode !== 'recent') {
            setDays(String(group.sync_recent_days ?? syncDefaults.recent_days));
          }
          void handleSave(nextMode, days);
        }}
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
        value={days}
        className={mode !== 'recent' ? 'account-sync-if-recent' : 'account-sync-if-recent is-visible'}
        disabled={mode !== 'recent'}
        onChange={(event) => setDays(event.target.value)}
        onBlur={() => { void handleSave(mode, days); }}
      />
    </div>
  );
}

export function GroupSyncToolbar() {
  const { state } = useGroupsState();
  const group = state.groups.find((item) => item.id === state.selectedGroupId);

  if (!group) return null;

  const toolbarKey = `${group.id}:${group.sync_mode || ''}:${group.sync_recent_days ?? ''}`;
  return <GroupSyncToolbarFields key={toolbarKey} group={group} />;
}
