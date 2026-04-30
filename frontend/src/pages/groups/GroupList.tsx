import { useState, useCallback } from 'react';
import { useGroupsState, useGroupsActions } from '../../store/groups';
import { useI18n } from '../../i18n';
import { GroupContextMenu } from './GroupContextMenu';
import type { Group } from '../../store/shared';

interface GroupListProps {
  onSync: (groupId: number) => void;
}

export function GroupList({ onSync }: GroupListProps) {
  const { state, dispatch } = useGroupsState();
  const { t } = useI18n();
  const [contextGroup, setContextGroup] = useState<Group | null>(null);
  const [contextPos, setContextPos] = useState({ x: 0, y: 0 });

  const selectGroup = useCallback(
    (groupId: number) => {
      dispatch({ type: 'SELECT_GROUP', groupId });
    },
    [dispatch],
  );

  const handleContextMenu = (e: React.MouseEvent, group: Group) => {
    e.preventDefault();
    setContextGroup(group);
    setContextPos({ x: e.clientX, y: e.clientY });
  };

  return (
    <>
      <div className="list group-list" id="group-list">
        {state.groups.map((group) => (
          <div
            key={group.id}
            className={`list-item group-list-item${state.selectedGroupId === group.id ? ' is-active' : ''}`}
            onClick={() => selectGroup(group.id)}
            onContextMenu={(e) => handleContextMenu(e, group)}
          >
            <div>
              <div className="account-name">{group.name}</div>
              <div className="account-sub">
                {group.account_count || 0} {t('groups.accounts', 'accounts')}
              </div>
            </div>
            <div className="group-item-actions">
              <button
                className="group-sync-btn"
                type="button"
                title={t('groups.syncNow', 'Sync')}
                aria-label={t('groups.syncNow', 'Sync')}
                onClick={(e) => {
                  e.stopPropagation();
                  onSync(group.id);
                }}
              >
                <svg viewBox="0 0 24 24" aria-hidden="true">
                  <path d="M12 6V3L8 7l4 4V8c2.2 0 4 1.8 4 4a4 4 0 0 1-4 4 4 4 0 0 1-3.8-2.6l-1.9.6A6 6 0 0 0 12 20a6 6 0 0 0 0-12z"/>
                </svg>
              </button>
              <span className="badge">{group.account_count || 0}</span>
            </div>
          </div>
        ))}
      </div>
      {contextGroup && (
        <GroupContextMenu
          groupId={contextGroup.id}
          x={contextPos.x}
          y={contextPos.y}
          onClose={() => setContextGroup(null)}
        />
      )}
    </>
  );
}
