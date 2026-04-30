import { useCallback } from 'react';
import { useArticlesState } from '../../store/articles';
import { useI18n } from '../../i18n';
import { apiGet } from '../../api';
import { ArticleFilterSummary } from './ArticleFilterSummary';
import { ArticleTypeFacets } from './ArticleTypeFacets';
import type { Group } from '../../store/shared';

export function ArticleFilters() {
  const { state, dispatch } = useArticlesState();
  const { t } = useI18n();
  const isNarrow = window.matchMedia('(max-width: 720px)').matches;

  const handleGroupChange = useCallback(async () => {
    const groupId = (document.getElementById('article-group-filter') as HTMLSelectElement)?.value || '';
    const select = document.getElementById('article-account-filter') as HTMLSelectElement | null;
    if (select) {
      select.innerHTML = '<option value="">' + t('filters.allAccounts', 'All Accounts') + '</option>';
      if (groupId) {
        const payload = await apiGet(`/api/account?group_id=${groupId}&page_size=200`);
        const accounts = (payload.accounts || []) as Array<{ biz: string; nickname: string }>;
        accounts.forEach((a) => {
          const opt = document.createElement('option');
          opt.value = a.biz;
          opt.textContent = a.nickname;
          select.appendChild(opt);
        });
      }
    }
  }, [t]);

  return (
    <div className={`article-filter-shell${isNarrow && state.filtersCollapsed ? ' is-mobile-collapsed' : ''}`} id="article-filter-shell">
      <div className="article-filter-stack">
        <div className="toolbar article-filter-grid article-filter-grid-top">
          <select id="article-group-filter" onChange={handleGroupChange}>
            <option value="">{t('filters.allGroups', 'All Groups')}</option>
          </select>
          <select id="article-account-filter">
            <option value="">{t('filters.allAccounts', 'All Accounts')}</option>
          </select>
        </div>
        <div className="toolbar article-filter-grid article-filter-grid-middle">
          <select id="article-type-filter">
            <option value="">{t('filters.allTypes', 'All Types')}</option>
            <option value="0">{t('articles.type.0', 'Regular Article')}</option>
            <option value="5">{t('articles.type.5', 'Video Share')}</option>
            <option value="6">{t('articles.type.6', 'Music Share')}</option>
            <option value="7">{t('articles.type.7', 'Audio Share')}</option>
            <option value="8">{t('articles.type.8', 'Picture Share')}</option>
            <option value="10">{t('articles.type.10', 'Text Share')}</option>
            <option value="11">{t('articles.type.11', 'Article Share')}</option>
            <option value="17">{t('articles.type.17', 'Short Post')}</option>
          </select>
          <select id="article-sort">
            <option value="publish_at_desc">{t('filters.sortPublishedDesc', 'Published (Newest)')}</option>
            <option value="relevance_desc">{t('filters.sortRelevanceDesc', 'Relevance')}</option>
          </select>
        </div>
        <div className="toolbar article-filter-grid article-filter-grid-bottom">
          <div className="input article-search-input">
            <input type="search" id="article-search" placeholder={t('filters.search', 'Search')} />
            <span className="icon">
              <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M15.5 14h-.79l-.28-.27A6 6 0 1 0 14 15.5l.27.28v.79L20 21.5 21.5 20l-6-6zm-5.5 0a4 4 0 1 1 0-8 4 4 0 0 1 0 8z"/></svg>
            </span>
          </div>
          <ArticleFilterSummary />
        </div>
        <ArticleTypeFacets />
      </div>
    </div>
  );
}
