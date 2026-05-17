import { useEffect, useCallback, useMemo, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { useGroupsState, useGroupsActions } from '../../store/groups';
import { GroupList } from './GroupList';
import { GroupHeader } from './GroupHeader';
import { GroupSyncToolbar } from './GroupSyncToolbar';
import { AccountCardGrid } from './AccountCardGrid';
import { BatchActions } from './BatchActions';
import { AccountSearchModal } from './AccountSearchModal';
import { GroupNameModal } from './GroupNameModal';
import { ConfirmModal } from '../../components/ConfirmModal';
import { useI18n } from '../../i18n';
import { apiSend, isAuthError } from '../../api';
import { useToast } from '../../hooks/useToast';
import { onRefresh } from '../../utils/events';

interface GroupNameDialogState {
  mode: 'create' | 'rename';
  groupId: number | null;
  initialName: string;
}

interface GroupDeleteDialogState {
  groupId: number;
}

const parseRouteGroupId = (value: string | null): number | null => {
  if (!value || !/^\d+$/.test(value)) {
    return null;
  }
  const numericValue = Number(value);
  return numericValue > 0 ? numericValue : null;
};

export function GroupsPage() {
  const { state, dispatch } = useGroupsState();
  const { loadGroups } = useGroupsActions();
  const { t } = useI18n();
  const { showToast } = useToast();
  const [searchParams, setSearchParams] = useSearchParams();
  const [accountQuery, setAccountQuery] = useState('');
  const [isSearchModalOpen, setIsSearchModalOpen] = useState(false);
  const [groupNameDialog, setGroupNameDialog] = useState<GroupNameDialogState | null>(null);
  const [groupDeleteDialog, setGroupDeleteDialog] = useState<GroupDeleteDialogState | null>(null);
  const routeGroupId = parseRouteGroupId(searchParams.get('group'));
  const routeGroupParam = useMemo(() => searchParams.get('group') || '', [searchParams]);

  const syncGroupRoute = useCallback((groupId: number | null) => {
    const nextGroup = groupId != null ? String(groupId) : '';
    if (routeGroupParam === nextGroup) return;

    const nextParams = new URLSearchParams(searchParams);
    if (nextGroup) {
      nextParams.set('group', nextGroup);
    } else {
      nextParams.delete('group');
    }
    setSearchParams(nextParams, { replace: true });
  }, [routeGroupParam, searchParams, setSearchParams]);

  const selectGroup = useCallback((groupId: number | null) => {
    dispatch({ type: 'SELECT_GROUP', groupId });
    syncGroupRoute(groupId);
  }, [dispatch, syncGroupRoute]);

  const refresh = useCallback(async () => {
    await loadGroups();
    // Load accounts will be triggered by the group selection effect
  }, [loadGroups]);

  // Init on mount
  useEffect(() => {
    void refresh();
  }, [refresh]);

  // Re-render group selects when needed
  useEffect(() => {
    const handler = () => {
      void loadGroups();
    };
    return onRefresh(handler);
  }, [loadGroups]);

  useEffect(() => {
    if (!state.groups.length) return;

    const groupExists = (groupId: number | null) => (
      groupId != null && state.groups.some((group) => group.id === groupId)
    );

    let nextGroupId = state.selectedGroupId;
    if (routeGroupId != null) {
      nextGroupId = groupExists(routeGroupId) ? routeGroupId : (groupExists(state.defaultGroupId) ? state.defaultGroupId : null);
    } else if (!groupExists(state.selectedGroupId)) {
      nextGroupId = groupExists(state.defaultGroupId) ? state.defaultGroupId : null;
    }

    if (nextGroupId !== state.selectedGroupId) {
      dispatch({ type: 'SELECT_GROUP', groupId: nextGroupId });
    }
    syncGroupRoute(nextGroupId);
  }, [dispatch, routeGroupId, state.defaultGroupId, state.groups, state.selectedGroupId, syncGroupRoute]);

  // Handle group sync actions
  const handleGroupSync = async (groupId: number) => {
    try {
      await apiSend('/api/settings/run', 'POST', { group_id: groupId });
      window.location.hash = '#/settings/sync';
      showToast(t('groups.syncTriggered', 'Group sync triggered.'));
    } catch (err) {
      if (isAuthError(err)) return;
      showToast(t('groups.syncTriggerFailed', 'Failed to trigger group sync.'));
    }
  };

  const handleSubmitGroupName = async (name: string) => {
    if (!groupNameDialog) return;
    if (groupNameDialog.mode === 'create') {
      await apiSend('/api/group', 'POST', { name });
      await loadGroups();
      return;
    }
    if (groupNameDialog.groupId == null) return;
    await apiSend(`/api/group/${groupNameDialog.groupId}`, 'PATCH', { name });
    await loadGroups();
  };

  const handleConfirmGroupDelete = async () => {
    if (!groupDeleteDialog) return;
    await apiSend(`/api/group/${groupDeleteDialog.groupId}`, 'DELETE', {});
    selectGroup(null);
    await loadGroups();
  };

  return (
    <section id="view-groups" className="view is-active">
      <div className="groups-layout">
        <div className="panel groups-sidebar">
          <div className="panel-header groups-sidebar-header">
            <div>
              <h2>{t('groups.title', 'Groups')}</h2>
              <p className="muted">{t('groups.subtitle', 'Manage accounts by group.')}</p>
            </div>
            <button
              className="btn"
              id="btn-group-create"
              type="button"
              onClick={() => setGroupNameDialog({ mode: 'create', groupId: null, initialName: '' })}
            >
              <span className="icon">
                <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M11 5h2v14h-2zM5 11h14v2H5z"/></svg>
              </span>
              <span>{t('groups.create', 'New Group')}</span>
            </button>
          </div>
          <GroupList onSync={handleGroupSync} onSelect={selectGroup} />
        </div>

        <div className="panel groups-main">
          <GroupHeader
            accountQuery={accountQuery}
            onAccountQueryChange={setAccountQuery}
            onOpenAccountSearch={() => setIsSearchModalOpen(true)}
            onOpenRename={(groupId, name) => setGroupNameDialog({ mode: 'rename', groupId, initialName: name })}
            onOpenDelete={(groupId) => setGroupDeleteDialog({ groupId })}
          />
          <GroupSyncToolbar />
          <BatchActions />
          <AccountCardGrid query={accountQuery} />
        </div>
      </div>

      <AccountSearchModal
        isOpen={isSearchModalOpen}
        onClose={() => setIsSearchModalOpen(false)}
      />
      {groupNameDialog && (
        <GroupNameModal
          key={`${groupNameDialog.mode}:${groupNameDialog.groupId ?? 'new'}:${groupNameDialog.initialName}`}
          isOpen
          title={groupNameDialog.mode === 'rename' ? t('groups.rename', 'Rename') : t('groups.create', 'New Group')}
          initialValue={groupNameDialog.initialName}
          placeholder={
            groupNameDialog.mode === 'rename'
              ? t('groups.renamePrompt', 'New group name')
              : t('groups.createPrompt', 'Group name')
          }
          submitLabel={groupNameDialog.mode === 'rename' ? t('common.save', '保存') : t('groups.create', 'New Group')}
          onClose={() => setGroupNameDialog(null)}
          onSubmit={handleSubmitGroupName}
        />
      )}
      {groupDeleteDialog && (
        <ConfirmModal
          isOpen
          title={t('groups.delete', 'Delete')}
          message={t('groups.deleteConfirm', 'Delete this group?')}
          confirmLabel={t('groups.delete', 'Delete')}
          onClose={() => setGroupDeleteDialog(null)}
          onConfirm={handleConfirmGroupDelete}
        />
      )}
    </section>
  );
}
