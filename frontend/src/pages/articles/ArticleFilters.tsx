import { useArticlesState } from '../../store/articles';
import { useI18n } from '../../i18n';
import { ArticleTypeFacets } from './ArticleTypeFacets';
import type {
  ArticleFiltersState,
  ArticleSelectOption,
} from './filtering';

interface ArticleFiltersProps {
  filters: ArticleFiltersState;
  groupOptions: ArticleSelectOption[];
  accountOptions: ArticleSelectOption[];
  onChange: (patch: Partial<ArticleFiltersState>) => void;
  onReset: () => void;
}

/**
 * The view switch and search stay on screen; the scope selects and the type
 * facets live in a drawer, because they are set once and then left alone.
 */
export function ArticleFilters({
  filters,
  groupOptions,
  accountOptions,
  onChange,
  onReset,
}: ArticleFiltersProps) {
  const { state, dispatch } = useArticlesState();
  const { t } = useI18n();
  const view = filters.starred ? 'starred' : filters.read === 'unread' ? 'unread' : 'all';

  return (
    <div className="article-filter-shell" id="article-filter-shell">
      <div className="article-filter-stack">
        <div className="toolbar article-filter-grid article-filter-grid-middle">
          <div className="article-view-switch" role="group" aria-label={t('articles.view', '视图')}>
            <button
              className={`article-view-option${view === 'all' ? ' is-active' : ''}`}
              id="btn-article-view-all"
              type="button"
              aria-pressed={view === 'all'}
              onClick={() => onChange({ read: '', starred: '' })}
            >
              {t('articles.viewAll', '全部')}
            </button>
            <button
              className={`article-view-option${view === 'unread' ? ' is-active' : ''}`}
              id="btn-article-view-unread"
              type="button"
              aria-pressed={view === 'unread'}
              onClick={() => onChange({ read: 'unread', starred: '' })}
            >
              {t('articles.viewUnread', '未读')}
            </button>
            <button
              className={`article-view-option${view === 'starred' ? ' is-active' : ''}`}
              id="btn-article-view-starred"
              type="button"
              aria-pressed={view === 'starred'}
              onClick={() => onChange({ read: '', starred: 'true' })}
            >
              {t('articles.viewStarred', '收藏')}
            </button>
          </div>
          <div className="input article-search-input">
            <input
              type='search'
              id='article-search'
              placeholder={t('filters.search', 'Search')}
              value={filters.search}
              onChange={(event) => onChange({ search: event.target.value })}
            />
            <span className="icon">
              <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M15.5 14h-.79l-.28-.27A6 6 0 1 0 14 15.5l.27.28v.79L20 21.5 21.5 20l-6-6zm-5.5 0a4 4 0 1 1 0-8 4 4 0 0 1 0 8z"/></svg>
            </span>
          </div>
        </div>
        <div
          className={`article-filter-drawer${state.filtersCollapsed ? '' : ' is-open'}`}
          id="article-filter-drawer"
        >
          <div className="toolbar article-filter-grid article-filter-grid-top">
            <select
              id="article-group-filter"
              value={filters.groupId}
              onChange={(event) => onChange({ groupId: event.target.value, accountBiz: '' })}
            >
              <option value=''>{t('filters.allGroups', 'All Groups')}</option>
              {groupOptions.map((group) => (
                <option key={group.value} value={group.value}>{group.label}</option>
              ))}
            </select>
            <select
              id="article-sort"
              value={filters.sort}
              onChange={(event) => onChange({ sort: event.target.value })}
            >
              <option value='publish_at_desc'>{t('filters.sortPublishedDesc', 'Published (Newest)')}</option>
              <option value='relevance_desc'>{t('filters.sortRelevanceDesc', 'Relevance')}</option>
            </select>
            <select
              id="article-account-filter"
              value={filters.accountBiz}
              onChange={(event) => onChange({ accountBiz: event.target.value })}
            >
              <option value=''>{t('filters.allAccounts', 'All Accounts')}</option>
              {accountOptions.map((account) => (
                <option key={account.value} value={account.value}>{account.label}</option>
              ))}
            </select>
          </div>
          <ArticleTypeFacets
            activeType={filters.itemShowType}
            onChange={(itemShowType) => {
              dispatch({ type: 'SET_TYPE_FACETS_EXPANDED', expanded: false });
              onChange({ itemShowType });
            }}
          />
          <button
            className="btn ghost article-reset-btn"
            id="btn-article-reset"
            type="button"
            onClick={onReset}
          >
            {t('articles.resetFilters', 'Reset Filters')}
          </button>
        </div>
      </div>
    </div>
  );
}
