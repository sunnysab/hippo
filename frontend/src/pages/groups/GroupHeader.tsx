import { useNavigate } from 'react-router-dom';
import { useGroupsState } from '../../store/groups';
import { useI18n } from '../../i18n';
import { copyToClipboard } from '../../utils/clipboard';
import { useToast } from '../../hooks/useToast';

interface GroupHeaderProps {
  accountQuery: string;
  onAccountQueryChange: (value: string) => void;
  onOpenAccountSearch: () => void;
  onOpenRename: (groupId: number, name: string) => void;
  onOpenDelete: (groupId: number) => void;
}

export function GroupHeader({
  accountQuery,
  onAccountQueryChange,
  onOpenAccountSearch,
  onOpenRename,
  onOpenDelete,
}: GroupHeaderProps) {
  const { state } = useGroupsState();
  const { t } = useI18n();
  const { showToast } = useToast();
  const navigate = useNavigate();

  const group = state.groups.find((g) => g.id === state.selectedGroupId);

  if (!group) {
    return (
      <div className="panel-header groups-main-header">
        <div>
          <div className="group-headline">
            <button className="group-name" id="group-current-name" type="button" disabled>
              {t('groups.currentTitle', 'Group')}
            </button>
            <button className="group-id" id="group-current-id" type="button" disabled>
              {t('groups.currentIdEmpty', 'ID')}
            </button>
          </div>
          <p className="muted" id="group-current-meta">{t('groups.currentEmpty', 'Select a group.')}</p>
        </div>
      </div>
    );
  }

  const handleNameClick = () => {
    navigate(`/articles?group=${group.id}`);
  };

  const handleViewArticles = () => {
    navigate(`/articles?group=${group.id}`);
  };

  const handleIdClick = async () => {
    try {
      await copyToClipboard(String(group.id));
      showToast(t('groups.idCopied', '分组 ID 已复制'));
    } catch { /* ignore */ }
  };

  return (
    <div className="panel-header groups-main-header">
      <div>
        <div className="group-headline">
          <button
            className="group-name"
            id="group-current-name"
            type="button"
            title={group.name}
            onClick={handleNameClick}
          >
            {group.name}
          </button>
          <button
            className="group-id"
            id="group-current-id"
            type="button"
            onClick={handleIdClick}
          >
            {t('groups.currentId', 'ID: {id}').replace('{id}', String(group.id))}
          </button>
        </div>
        <p className="muted" id="group-current-meta">
          {t('groups.currentMeta', '{accounts} accounts · {articles} articles')
            .replace('{accounts}', String(group.account_count || 0))
            .replace('{articles}', String(group.article_count || 0))}
        </p>
      </div>
      <div className="toolbar group-actions">
        <div className="input">
          <input
            type="search"
            id="account-search"
            placeholder={t('filters.search', 'Search')}
            value={accountQuery}
            onChange={(event) => onAccountQueryChange(event.target.value)}
          />
          <span className="icon">
            <svg viewBox="0 0 24 24" aria-hidden="true">
              <path d="M15.5 14h-.79l-.28-.27A6 6 0 1 0 14 15.5l.27.28v.79L20 21.5 21.5 20l-6-6zm-5.5 0a4 4 0 1 1 0-8 4 4 0 0 1 0 8z"/>
            </svg>
          </span>
        </div>
        <button className="btn ghost" id="btn-account-add" type="button" onClick={onOpenAccountSearch}>
          <span className="icon">
            <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M11 5h2v14h-2zM5 11h14v2H5z"/></svg>
          </span>
          <span>{t('accounts.add', 'Add Account')}</span>
        </button>
        <button className="btn ghost" id="btn-group-rename" type="button" onClick={() => onOpenRename(group.id, group.name)}>
          {t('groups.rename', 'Rename')}
        </button>
        <button className="btn ghost" id="btn-group-delete" type="button" onClick={() => onOpenDelete(group.id)}>
          {t('groups.delete', 'Delete')}
        </button>
        <button className="btn ghost" id="btn-group-articles" type="button" onClick={handleViewArticles}>
          {t('groups.viewArticles', 'View group articles')}
        </button>
      </div>
    </div>
  );
}
