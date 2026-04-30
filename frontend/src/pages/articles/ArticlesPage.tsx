import { useEffect, useCallback } from 'react';
import { useSearchParams } from 'react-router-dom';
import { useArticlesState } from '../../store/articles';
import { useGroupsState } from '../../store/groups';
import { useI18n } from '../../i18n';
import { useToast } from '../../hooks/useToast';
import { useReaderSettings } from '../../hooks/useReaderSettings';
import { apiGet, apiSend } from '../../api';
import { ArticleFilters } from './ArticleFilters';
import { ArticleList } from './ArticleList';
import { ArticlePreview } from './ArticlePreview';
import { ArticleResizer } from './ArticleResizer';
import { LoadingIndicator } from '../../components/LoadingIndicator';
import type { Article, ArticlePayload } from '../../store/articles';
import { ARTICLE_SORT_PUBLISH_AT_DESC, ARTICLE_SORT_RELEVANCE_DESC, ARTICLE_FILTER_PARAM_KEYS } from '../../utils/constants';
import { copyToClipboard } from '../../utils/clipboard';

export function ArticlesPage() {
  const { state, dispatch } = useArticlesState();
  const groupsState = useGroupsState();
  const { t } = useI18n();
  const { showToast } = useToast();
  const { config, updateConfig } = useReaderSettings();
  const [searchParams, setSearchParams] = useSearchParams();

  const isNarrowViewport = () => window.matchMedia('(max-width: 720px)').matches;

  // Sync filter URL params with state
  const applyUrlParams = useCallback(async () => {
    const groupId = searchParams.get('group') || '';
    const accountBiz = searchParams.get('account') || '';
    const itemShowType = searchParams.get('type') || '';
    const search = searchParams.get('q') || '';
    const sort = searchParams.get('sort') || '';

    const groupFilter = document.getElementById('article-group-filter') as HTMLSelectElement | null;
    const accountFilter = document.getElementById('article-account-filter') as HTMLSelectElement | null;
    const searchInput = document.getElementById('article-search') as HTMLInputElement | null;
    const sortFilter = document.getElementById('article-sort') as HTMLSelectElement | null;
    const typeFilter = document.getElementById('article-type-filter') as HTMLSelectElement | null;

    if (groupId && groupFilter) groupFilter.value = groupId;
    if (accountBiz && accountFilter) accountFilter.value = accountBiz;
    if (itemShowType && typeFilter) typeFilter.value = itemShowType;
    if (search && searchInput) searchInput.value = search;
    const hasSearch = Boolean(search && search.trim());
    if (sortFilter) {
      sortFilter.value = sort || (hasSearch ? ARTICLE_SORT_RELEVANCE_DESC : ARTICLE_SORT_PUBLISH_AT_DESC);
    }
    await loadArticles(true);
  }, [searchParams]);

  const updateUrlParams = useCallback(() => {
    const groupFilter = document.getElementById('article-group-filter') as HTMLSelectElement | null;
    const accountFilter = document.getElementById('article-account-filter') as HTMLSelectElement | null;
    const searchInput = document.getElementById('article-search') as HTMLInputElement | null;
    const sortFilter = document.getElementById('article-sort') as HTMLSelectElement | null;
    const typeFilter = document.getElementById('article-type-filter') as HTMLSelectElement | null;

    const groupId = groupFilter?.value || '';
    const accountBiz = accountFilter?.value || '';
    const itemShowType = typeFilter?.value || '';
    const search = searchInput?.value.trim() || '';
    const sort = sortFilter?.value || '';

    const params = new URLSearchParams();
    if (groupId) params.set('group', groupId);
    if (accountBiz) params.set('account', accountBiz);
    if (itemShowType) params.set('type', itemShowType);
    if (search) params.set('q', search);
    const hasSearch = Boolean(search);
    const defaultSort = hasSearch ? ARTICLE_SORT_RELEVANCE_DESC : ARTICLE_SORT_PUBLISH_AT_DESC;
    if (sort && sort !== defaultSort) params.set('sort', sort);

    setSearchParams(params, { replace: true });
  }, [setSearchParams]);

  const loadArticles = useCallback(async (reset = true) => {
    if (state.isArticleLoading) return;
    dispatch({ type: 'SET_LOADING', loading: true });

    const groupFilter = document.getElementById('article-group-filter') as HTMLSelectElement | null;
    const accountFilter = document.getElementById('article-account-filter') as HTMLSelectElement | null;
    const searchInput = document.getElementById('article-search') as HTMLInputElement | null;
    const sortFilter = document.getElementById('article-sort') as HTMLSelectElement | null;
    const typeFilter = document.getElementById('article-type-filter') as HTMLSelectElement | null;

    const groupId = groupFilter?.value;
    const accountBiz = accountFilter?.value;
    const itemShowType = typeFilter?.value;
    const search = searchInput?.value.trim();
    const sort = sortFilter?.value || ARTICLE_SORT_PUBLISH_AT_DESC;
    const page = reset ? 1 : state.articlePage;
    const pageItems = reset ? [] : state.articles;

    try {
      const url = new URL('/api/article', window.location.origin);
      if (groupId) url.searchParams.set('group_id', groupId);
      if (accountBiz) url.searchParams.set('biz', accountBiz);
      if (itemShowType) url.searchParams.set('item_show_type', itemShowType);
      if (search) url.searchParams.set('q', search);
      url.searchParams.set('sort', sort);
      url.searchParams.set('page', String(page));
      url.searchParams.set('page_size', String(state.articlePageSize));

      const payload = await apiGet(url.pathname + url.search);
      const newArticles = (payload.articles || []) as Article[];
      const hasMore = newArticles.length >= state.articlePageSize;

      dispatch({
        type: 'SET_ARTICLES',
        articles: reset ? newArticles : [...pageItems, ...newArticles],
        reset,
      });
      dispatch({ type: 'SET_PAGE', page: reset ? 2 : state.articlePage + 1, hasMore });

      // Update facet payload
      dispatch({
        type: 'SET_FACET_PAYLOAD',
        payload: {
          total: payload.total as number,
          item_show_type_facets: (payload.item_show_type_facets || []) as Array<{ item_show_type: number; count: number }>,
        },
      });
    } finally {
      dispatch({ type: 'SET_LOADING', loading: false });
    }
  }, [state.isArticleLoading, state.articlePage, state.articlePageSize, state.articles, dispatch]);

  const selectArticle = useCallback(async (id: number) => {
    dispatch({ type: 'SELECT_ARTICLE', id, payload: null });
    const payload = await apiGet(`/api/article/${id}`);
    dispatch({ type: 'SELECT_ARTICLE', id, payload: payload as unknown as ArticlePayload });
    if (isNarrowViewport()) {
      dispatch({ type: 'SET_MOBILE_READING', reading: true });
    }
  }, [dispatch]);

  const resetFilters = useCallback(async () => {
    const groupFilter = document.getElementById('article-group-filter') as HTMLSelectElement | null;
    const accountFilter = document.getElementById('article-account-filter') as HTMLSelectElement | null;
    const typeFilter = document.getElementById('article-type-filter') as HTMLSelectElement | null;
    const sortFilter = document.getElementById('article-sort') as HTMLSelectElement | null;
    const searchInput = document.getElementById('article-search') as HTMLInputElement | null;

    if (groupFilter) groupFilter.value = '';
    if (accountFilter) accountFilter.value = '';
    if (typeFilter) typeFilter.value = '';
    if (searchInput) searchInput.value = '';
    if (sortFilter) sortFilter.value = ARTICLE_SORT_PUBLISH_AT_DESC;
    dispatch({ type: 'SELECT_ARTICLE', id: null, payload: null });
    updateUrlParams();
    await loadArticles(true);
  }, [loadArticles, updateUrlParams, dispatch]);

  // Init on mount
  useEffect(() => {
    // Load group options
    const loadGroups = async () => {
      const payload = await apiGet('/api/group');
      const groups = (payload.groups || []) as Array<{ id: number; name: string; account_count: number }>;
      const select = document.getElementById('article-group-filter') as HTMLSelectElement | null;
      if (select) {
        const currentValue = select.value;
        select.innerHTML = '<option value="">' + t('filters.allGroups', 'All Groups') + '</option>';
        groups.forEach((g) => {
          const opt = document.createElement('option');
          opt.value = String(g.id);
          opt.textContent = g.name;
          select.appendChild(opt);
        });
        if (currentValue) select.value = currentValue;
      }
    };

    void loadGroups().then(() => {
      const params = new URLSearchParams(window.location.hash.split('?')[1] || '');
      if (params.toString()) {
        void applyUrlParams();
      } else {
        void loadArticles(true);
      }
    });
  }, []);

  // Refresh handler
  useEffect(() => {
    const handler = () => {
      void loadArticles(true);
    };
    window.addEventListener('hippo:refresh', handler);
    return () => window.removeEventListener('hippo:refresh', handler);
  }, [loadArticles]);

  const getSelectedArticleLink = () => {
    const article = state.currentArticlePayload?.article ||
      state.articles.find((a) => a.id === state.selectedArticleId);
    return article?.source_url || (article as unknown as Record<string, string>)?.link || '';
  };

  return (
    <section id="view-articles" className="view is-active">
      <div className={`article-layout${state.filtersCollapsed ? '' : ''}${state.mobileReading ? ' is-mobile-reading' : ''}`}>
        <div className="panel article-list">
          <div className="panel-header article-sidebar-header">
            <div>
              <div className="section-kicker">{t('articles.libraryKicker', 'Library')}</div>
              <h2>{t('articles.title', 'Articles')}</h2>
              <p className="muted">{t('articles.librarySubtitle', 'Filter, scan, and open synced articles.')}</p>
            </div>
            <div className="toolbar article-sidebar-actions">
              <button
                className="btn ghost article-mobile-toggle"
                id="btn-article-filter-toggle"
                type="button"
                onClick={() => dispatch({ type: 'SET_FILTERS_COLLAPSED', collapsed: !state.filtersCollapsed })}
              >
                {state.filtersCollapsed ? t('articles.showFilters', 'Expand Filters') : t('articles.hideFilters', 'Collapse Filters')}
              </button>
              <button
                className="btn ghost article-reset-btn"
                id="btn-article-reset"
                type="button"
                onClick={resetFilters}
              >
                {t('articles.resetFilters', 'Reset Filters')}
              </button>
            </div>
          </div>
          <ArticleFilters />
          <LoadingIndicator isLoading={state.isArticleLoading} id="article-search-loading" />
          <ArticleList onSelect={selectArticle} />
        </div>

        <ArticleResizer />

        <div className="panel article-preview">
          <div className="panel-header article-preview-topbar">
            <div>
              <div className="section-kicker">{t('articles.previewKicker', 'Reader')}</div>
              <h2>{t('articles.previewTitle', 'Reading Preview')}</h2>
              <p className="muted">{t('articles.previewSubtitle', 'Inspect metadata and tune typography for long-form reading.')}</p>
            </div>
            <div className="toolbar article-preview-toolbar">
              <button
                className="btn ghost article-mobile-back"
                id="btn-article-mobile-back"
                type="button"
                onClick={() => dispatch({ type: 'SET_MOBILE_READING', reading: false })}
              >
                {t('articles.backToList', 'Back to list')}
              </button>
              <button
                className="btn ghost article-toolbar-btn"
                id="btn-article-open-original"
                type="button"
                disabled={!getSelectedArticleLink()}
                onClick={() => { const link = getSelectedArticleLink(); if (link) window.open(link, '_blank', 'noopener,noreferrer'); }}
              >
                {t('articles.menu.openOriginal', 'Open original')}
              </button>
              <button
                className="btn ghost article-toolbar-btn"
                id="btn-article-copy-link"
                type="button"
                disabled={!getSelectedArticleLink()}
                onClick={async () => {
                  const link = getSelectedArticleLink();
                  if (!link) return;
                  await copyToClipboard(link);
                  showToast(t('articles.linkCopied', 'Link copied.'));
                }}
              >
                {t('articles.menu.copyLink', 'Copy link')}
              </button>
              <button className="icon-btn" id="reader-copy" type="button" aria-label="Copy article text" title="Copy article text" onClick={async () => {
                const content = document.querySelector('.article-preview-body .reader');
                if (content) {
                  const text = (content as HTMLElement).innerText;
                  try { await copyToClipboard(text); showToast(t('articles.copied', '已复制全文')); } catch { showToast(t('articles.copyFailed', '复制失败')); }
                }
              }}>
                <span className="icon"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M16 1H4c-1.1 0-2 .9-2 2v14h2V3h12V1zm3 4H8c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h11c1.1 0 2-.9 2-2V7c0-1.1-.9-2-2-2zm0 16H8V7h11v14z"/></svg></span>
              </button>
              <button className="icon-btn" id="reader-toggle" type="button" aria-label="Typography settings" title="Typography settings">
                <span className="icon"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 6h16v2H4zM4 11h16v2H4zM4 16h16v2H4z"/></svg></span>
              </button>
              <button className="icon-btn" id="btn-article-toggle" type="button" aria-label="Toggle list" title="Toggle list">
                <span className="icon"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 6h16v2H4zM4 11h10v2H4zM4 16h16v2H4z"/></svg></span>
              </button>
            </div>
          </div>
          <div className="reader-controls is-hidden" id="reader-controls">
            <label>
              <span>{t('reader.width', 'Width')}</span>
              <input id="reader-width" type="range" min="400" max="1200" step="10" value={config.width} onChange={(e) => updateConfig({ width: e.target.value })} />
            </label>
            <label>
              <span>{t('reader.font', 'Font Size')}</span>
              <input id="reader-font" type="range" min="12" max="32" step="1" value={config.font} onChange={(e) => updateConfig({ font: e.target.value })} />
            </label>
            <label>
              <span>{t('reader.lineHeight', 'Line Height')}</span>
              <input id="reader-line" type="range" min="1.2" max="3.0" step="0.05" value={config.lineHeight} onChange={(e) => updateConfig({ lineHeight: e.target.value })} />
            </label>
            <label>
              <span>{t('reader.letter', 'Letter Spacing')}</span>
              <input id="reader-letter" type="range" min="0" max="4" step="0.1" value={config.letter} onChange={(e) => updateConfig({ letter: e.target.value })} />
            </label>
            <label className="switch">
              <input id="reader-serif" type="checkbox" checked={config.serif} onChange={(e) => updateConfig({ serif: e.target.checked })} />
              <span>{t('reader.serif', 'Serif Font')}</span>
            </label>
            <label className="switch">
              <input id="reader-hide-small-images" type="checkbox" checked={config.hideSmall} onChange={(e) => updateConfig({ hideSmall: e.target.checked })} />
              <span>{t('reader.hideSmallImages', 'Hide Small Images')}</span>
            </label>
          </div>
          <ArticlePreview />
        </div>
      </div>

      {/* Context menus */}
      <div className="context-menu is-hidden" id="article-context-menu">
        <button className="context-item" id="article-menu-open" type="button">{t('articles.menu.openOriginal', 'Open original')}</button>
        <button className="context-item" id="article-menu-copy" type="button">{t('articles.menu.copyLink', 'Copy link')}</button>
      </div>
      <div className="context-menu is-hidden" id="article-image-context-menu">
        <button className="context-item" id="article-image-menu-block" type="button">{t('articles.menu.blockImage', 'Block image')}</button>
      </div>
    </section>
  );
}
