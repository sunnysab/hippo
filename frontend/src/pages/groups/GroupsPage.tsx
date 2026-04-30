import { useEffect, useCallback, useState } from 'react';
import { useGroupsState, useGroupsActions } from '../../store/groups';
import { GroupList } from './GroupList';
import { GroupHeader } from './GroupHeader';
import { GroupSyncToolbar } from './GroupSyncToolbar';
import { AccountCardGrid } from './AccountCardGrid';
import { BatchActions } from './BatchActions';
import { AccountSearchModal } from './AccountSearchModal';
import { useI18n } from '../../i18n';
import { apiSend } from '../../api';
import { useToast } from '../../hooks/useToast';

export function GroupsPage() {
  const { state, dispatch } = useGroupsState();
  const { loadGroups } = useGroupsActions();
  const { t } = useI18n();
  const { showToast } = useToast();
  const [accountQuery, setAccountQuery] = useState('');
  const [isSearchModalOpen, setIsSearchModalOpen] = useState(false);

  const refresh = useCallback(async () => {
    const { nextGroup } = await loadGroups();
    if (nextGroup && nextGroup !== state.selectedGroupId) {
      dispatch({ type: 'SELECT_GROUP', groupId: nextGroup });
    }
    // Load accounts will be triggered by the group selection effect
  }, [loadGroups, state.selectedGroupId, dispatch]);

  // Init on mount
  useEffect(() => {
    void refresh();
  }, [refresh]);

  // Re-render group selects when needed
  useEffect(() => {
    const handler = () => {
      void loadGroups();
    };
    window.addEventListener('hippo:refresh', handler);
    return () => window.removeEventListener('hippo:refresh', handler);
  }, [loadGroups]);

  // Handle group sync actions
  const handleGroupSync = async (groupId: number) => {
    try {
      await apiSend('/api/settings/run', 'POST', { group_id: groupId });
      window.location.hash = '#/settings';
      showToast(t('groups.syncTriggered', 'Group sync triggered.'));
    } catch {
      showToast(t('groups.syncTriggerFailed', 'Failed to trigger group sync.'));
    }
  };

  return (
    <section id="view-groups" className="view is-active">
      <div className="groups-layout">
        <div className="panel groups-sidebar">
          <div className="panel-header groups-sidebar-header">
            <div>
              <div className="section-kicker">
                {t('groups.collectionKicker', 'Collections')}
              </div>
              <h2>{t('groups.title', 'Groups')}</h2>
              <p className="muted">{t('groups.subtitle', 'Organize accounts into focused collections.')}</p>
            </div>
            <button
              className="btn"
              id="btn-group-create"
              type="button"
              onClick={async () => {
                const name = prompt(t('groups.createPrompt', 'Group name'));
                if (!name) return;
                await apiSend('/api/group', 'POST', { name });
                await loadGroups();
              }}
            >
              <span className="icon">
                <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M11 5h2v14h-2zM5 11h14v2H5z"/></svg>
              </span>
              <span>{t('groups.create', 'New Group')}</span>
            </button>
          </div>
          <GroupList onSync={handleGroupSync} />
        </div>

        <div className="panel groups-main">
          <GroupHeader
            accountQuery={accountQuery}
            onAccountQueryChange={setAccountQuery}
            onOpenAccountSearch={() => setIsSearchModalOpen(true)}
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
    </section>
  );
}
