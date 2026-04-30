import { useDeferredValue, useEffect, useCallback, useRef, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { useArticlesState } from '../../store/articles';
import { useI18n } from '../../i18n';
import { useToast } from '../../hooks/useToast';
import { useReaderSettings } from '../../hooks/useReaderSettings';
import { apiGet } from '../../api';
import { ArticleFilters } from './ArticleFilters';
import { ArticleList } from './ArticleList';
import { ArticlePreview } from './ArticlePreview';
import { ArticleResizer } from './ArticleResizer';
import { LoadingIndicator } from '../../components/LoadingIndicator';
import type { Article, ArticlePayload } from '../../store/articles';
import { ARTICLE_SORT_PUBLISH_AT_DESC } from '../../utils/constants';
import { copyToClipboard } from '../../utils/clipboard';
import {
  areArticleFiltersEqual,
  buildArticleRouteStateFromSearchParams,
  buildArticleSearchParams,
  type ArticleFiltersState,
  type ArticleSelectOption,
} from './filtering';

export function ArticlesPage() {
  const { state, dispatch } = useArticlesState();
  const { t } = useI18n();
  const { showToast } = useToast();
  const { config, updateConfig } = useReaderSettings();
  const [searchParams, setSearchParams] = useSearchParams();
  const routeState = buildArticleRouteStateFromSearchParams(searchParams);
  const initialRouteState = routeState;
  const {
    groupId: routeGroupId,
    accountBiz: routeAccountBiz,
    itemShowType: routeItemShowType,
    search: routeSearch,
    sort: routeSort,
  } = routeState.filters;
  const [filters, setFilters] = useState<ArticleFiltersState>(() => initialRouteState.filters);
  const [articleIdParam, setArticleIdParam] = useState(initialRouteState.articleId);
  const [wxArticleIdParam, setWxArticleIdParam] = useState(initialRouteState.wxArticleId);
  const [groupOptions, setGroupOptions] = useState<ArticleSelectOption[]>([]);
  const [accountOptions, setAccountOptions] = useState<ArticleSelectOption[]>([]);
  const [listCollapsed, setListCollapsed] = useState(false);
  const [readerControlsOpen, setReaderControlsOpen] = useState(false);
  const deferredSearch = useDeferredValue(filters.search.trim());
  const readerControlsRef = useRef<HTMLDivElement>(null);
  const readerToggleRef = useRef<HTMLButtonElement>(null);
  const previewRef = useRef<HTMLDivElement>(null);
  const isArticleLoadingRef = useRef(state.isArticleLoading);
  const articlePageRef = useRef(state.articlePage);
  const articlePageSizeRef = useRef(state.articlePageSize);
  const articlesRef = useRef(state.articles);
  const filtersRef = useRef(filters);
  const articleIdParamRef = useRef(articleIdParam);
  const wxArticleIdParamRef = useRef(wxArticleIdParam);
  const deferredSearchRef = useRef(deferredSearch);
  const skipNextArticleTargetResolveRef = useRef(false);
  const pendingRouteTargetRef = useRef<{ articleId: string; wxArticleId: string } | null>(null);

  const isNarrowViewport = () => window.matchMedia('(max-width: 720px)').matches;

  useEffect(() => {
    isArticleLoadingRef.current = state.isArticleLoading;
    articlePageRef.current = state.articlePage;
    articlePageSizeRef.current = state.articlePageSize;
    articlesRef.current = state.articles;
    filtersRef.current = filters;
    articleIdParamRef.current = articleIdParam;
    wxArticleIdParamRef.current = wxArticleIdParam;
    deferredSearchRef.current = deferredSearch;
  }, [
    articleIdParam,
    deferredSearch,
    filters,
    state.articlePage,
    state.articlePageSize,
    state.articles,
    state.isArticleLoading,
    wxArticleIdParam,
  ]);

  const loadArticles = useCallback(async (
    nextFilters: ArticleFiltersState,
    reset = true,
  ) => {
    if (isArticleLoadingRef.current) return;
    dispatch({ type: 'SET_LOADING', loading: true });
    isArticleLoadingRef.current = true;
    const sort = nextFilters.sort || ARTICLE_SORT_PUBLISH_AT_DESC;
    const page = reset ? 1 : articlePageRef.current;
    const pageSize = articlePageSizeRef.current;
    const previousArticles = articlesRef.current;

    try {
      const url = new URL('/api/article', window.location.origin);
      if (nextFilters.groupId) url.searchParams.set('group_id', nextFilters.groupId);
      if (nextFilters.accountBiz) url.searchParams.set('biz', nextFilters.accountBiz);
      if (nextFilters.itemShowType) url.searchParams.set('item_show_type', nextFilters.itemShowType);
      if (nextFilters.search) url.searchParams.set('q', nextFilters.search);
      url.searchParams.set('sort', sort);
      url.searchParams.set('page', String(page));
      url.searchParams.set('page_size', String(pageSize));

      const payload = await apiGet(url.pathname + url.search);
      const newArticles = (payload.articles || []) as Article[];
      const hasMore = newArticles.length >= pageSize;
      const mergedArticles = reset ? newArticles : [...previousArticles, ...newArticles];

      articlesRef.current = mergedArticles;
      articlePageRef.current = reset ? 2 : page + 1;

      dispatch({
        type: 'SET_ARTICLES',
        articles: mergedArticles,
        reset,
      });
      dispatch({ type: 'SET_PAGE', page: articlePageRef.current, hasMore });

      dispatch({
        type: 'SET_FACET_PAYLOAD',
        payload: {
          total: payload.total as number,
          item_show_type_facets: (payload.item_show_type_facets || []) as Array<{ item_show_type: number; count: number }>,
        },
      });
    } finally {
      isArticleLoadingRef.current = false;
      dispatch({ type: 'SET_LOADING', loading: false });
    }
  }, [dispatch]);

  const syncUrlParams = useCallback((nextFilters: ArticleFiltersState) => {
    setSearchParams(buildArticleSearchParams(nextFilters, {
      articleId: articleIdParam,
      wxArticleId: wxArticleIdParam,
    }), { replace: true });
  }, [articleIdParam, setSearchParams, wxArticleIdParam]);

  const updateFilters = useCallback((patch: Partial<ArticleFiltersState>) => {
    if (patch.groupId !== undefined) {
      setAccountOptions([]);
    }
    pendingRouteTargetRef.current = { articleId: '', wxArticleId: '' };
    setArticleIdParam('');
    setWxArticleIdParam('');
    setFilters((prev) => {
      const groupChanged = patch.groupId !== undefined && patch.groupId !== prev.groupId;
      return {
        ...prev,
        ...patch,
        ...(groupChanged ? { accountBiz: '' } : null),
      };
    });
  }, []);

  const loadGroupOptions = useCallback(async () => {
    const payload = await apiGet('/api/group');
    const groups = (payload.groups || []) as Array<{ id: number; name: string; account_count: number }>;
    const options = groups.map((group) => ({
      value: String(group.id),
      label: group.name,
    }));
    setGroupOptions(options);
    setAccountOptions((prev) => {
      if (!filtersRef.current.groupId) return [];
      const exists = options.some((group) => group.value === filtersRef.current.groupId);
      return exists ? prev : [];
    });
    setFilters((prev) => {
      if (!prev.groupId) return prev;
      const exists = options.some((group) => group.value === prev.groupId);
      return exists ? prev : { ...prev, groupId: '', accountBiz: '' };
    });
  }, []);

  const loadAccountOptions = useCallback(async (groupId: string) => {
    if (!groupId) return;

    const payload = await apiGet(`/api/account?group_id=${groupId}&page_size=200`);
    const accounts = (payload.accounts || []) as Array<{ biz: string; nickname: string }>;
    const options = accounts.map((account) => ({
      value: account.biz,
      label: account.nickname,
    }));
    setAccountOptions(options);
    setFilters((prev) => {
      if (!prev.accountBiz) return prev;
      const exists = options.some((account) => account.value === prev.accountBiz);
      return exists ? prev : { ...prev, accountBiz: '' };
    });
  }, []);

  const selectArticle = useCallback(async (id: number) => {
    skipNextArticleTargetResolveRef.current = true;
    pendingRouteTargetRef.current = { articleId: String(id), wxArticleId: '' };
    setArticleIdParam(String(id));
    setWxArticleIdParam('');
    dispatch({ type: 'SELECT_ARTICLE', id, payload: null });
    const payload = await apiGet(`/api/article/${id}`);
    dispatch({ type: 'SELECT_ARTICLE', id, payload: payload as unknown as ArticlePayload });
    if (isNarrowViewport()) {
      dispatch({ type: 'SET_MOBILE_READING', reading: true });
    }
  }, [dispatch]);

  const resetFilters = useCallback(async () => {
    setFilters({
      groupId: '',
      accountBiz: '',
      itemShowType: '',
      search: '',
      sort: ARTICLE_SORT_PUBLISH_AT_DESC,
    });
    setAccountOptions([]);
    pendingRouteTargetRef.current = { articleId: '', wxArticleId: '' };
    setArticleIdParam('');
    setWxArticleIdParam('');
    dispatch({ type: 'SELECT_ARTICLE', id: null, payload: null });
  }, [dispatch]);

  const resolveArticleTarget = useCallback(async () => {
    if (skipNextArticleTargetResolveRef.current) {
      skipNextArticleTargetResolveRef.current = false;
      return;
    }

    const articleId = articleIdParam.trim();
    const wxArticleId = wxArticleIdParam.trim();

    if (articleId) {
      if (!/^\d+$/.test(articleId)) {
        dispatch({ type: 'SELECT_ARTICLE', id: null, payload: null });
        return;
      }
      dispatch({ type: 'SELECT_ARTICLE', id: Number(articleId), payload: null });
      const payload = await apiGet(`/api/article/${articleId}`);
      dispatch({ type: 'SELECT_ARTICLE', id: Number(articleId), payload: payload as unknown as ArticlePayload });
      if (isNarrowViewport()) {
        dispatch({ type: 'SET_MOBILE_READING', reading: true });
      }
      return;
    }

    if (!wxArticleId) {
      dispatch({ type: 'SELECT_ARTICLE', id: null, payload: null });
      return;
    }

    const url = new URL('/api/article', window.location.origin);
    url.searchParams.set('article_id', wxArticleId);
    url.searchParams.set('page', '1');
    url.searchParams.set('page_size', '1');
    url.searchParams.set('sort', ARTICLE_SORT_PUBLISH_AT_DESC);
    const payload = await apiGet(url.pathname + url.search);
    const article = ((payload.articles || []) as Article[])[0];
    if (!article) {
      dispatch({ type: 'SELECT_ARTICLE', id: null, payload: null });
      return;
    }

    dispatch({ type: 'SELECT_ARTICLE', id: article.id, payload: null });
    const detailPayload = await apiGet(`/api/article/${article.id}`);
    dispatch({
      type: 'SELECT_ARTICLE',
      id: article.id,
      payload: detailPayload as unknown as ArticlePayload,
    });
    if (isNarrowViewport()) {
      dispatch({ type: 'SET_MOBILE_READING', reading: true });
    }
  }, [articleIdParam, dispatch, wxArticleIdParam]);

  useEffect(() => {
    const timer = window.setTimeout(() => {
      const pendingRouteTarget = pendingRouteTargetRef.current;
      if (pendingRouteTarget) {
        const routeMatchesPending = (
          routeState.articleId === pendingRouteTarget.articleId &&
          routeState.wxArticleId === pendingRouteTarget.wxArticleId
        );
        if (!routeMatchesPending) {
          return;
        }
        pendingRouteTargetRef.current = null;
      }

      const nextRouteFilters: ArticleFiltersState = {
        groupId: routeGroupId,
        accountBiz: routeAccountBiz,
        itemShowType: routeItemShowType,
        search: routeSearch,
        sort: routeSort,
      };

      if (filtersRef.current.groupId !== routeGroupId) {
        setAccountOptions([]);
      }
      if (!areArticleFiltersEqual(filtersRef.current, nextRouteFilters)) {
        setFilters(nextRouteFilters);
      }
      if (articleIdParamRef.current !== routeState.articleId) {
        setArticleIdParam(routeState.articleId);
      }
      if (wxArticleIdParamRef.current !== routeState.wxArticleId) {
        setWxArticleIdParam(routeState.wxArticleId);
      }
    }, 0);
    return () => window.clearTimeout(timer);
  }, [
    routeAccountBiz,
    routeState.articleId,
    routeGroupId,
    routeItemShowType,
    routeSearch,
    routeSort,
    routeState.wxArticleId,
  ]);

  useEffect(() => {
    const timer = window.setTimeout(() => {
      void loadGroupOptions();
    }, 0);
    return () => window.clearTimeout(timer);
  }, [loadGroupOptions]);

  useEffect(() => {
    if (!filters.groupId) return;
    const timer = window.setTimeout(() => {
      void loadAccountOptions(filters.groupId);
    }, 0);
    return () => window.clearTimeout(timer);
  }, [filters.groupId, loadAccountOptions]);

  useEffect(() => {
    syncUrlParams(filters);
  }, [filters, syncUrlParams]);

  useEffect(() => {
    const nextFilters: ArticleFiltersState = {
      groupId: filters.groupId,
      accountBiz: filters.accountBiz,
      itemShowType: filters.itemShowType,
      search: deferredSearch,
      sort: filters.sort,
    };
    const timer = window.setTimeout(() => {
      void loadArticles(nextFilters, true);
    }, 0);
    return () => window.clearTimeout(timer);
  }, [
    deferredSearch,
    filters.accountBiz,
    filters.groupId,
    filters.itemShowType,
    filters.sort,
    loadArticles,
  ]);

  useEffect(() => {
    const timer = window.setTimeout(() => {
      void resolveArticleTarget();
    }, 0);
    return () => window.clearTimeout(timer);
  }, [resolveArticleTarget]);

  useEffect(() => {
    const handler = () => {
      const nextFilters = filtersRef.current;
      const nextSearch = deferredSearchRef.current;
      void loadGroupOptions();
      if (nextFilters.groupId) {
        void loadAccountOptions(nextFilters.groupId);
      }
      void loadArticles({ ...nextFilters, search: nextSearch }, true);
      void resolveArticleTarget();
    };
    window.addEventListener('hippo:refresh', handler);
    return () => window.removeEventListener('hippo:refresh', handler);
  }, [loadAccountOptions, loadArticles, loadGroupOptions, resolveArticleTarget]);

  useEffect(() => {
    if (!readerControlsOpen) return;

    const handlePointerDown = (event: PointerEvent) => {
      const target = event.target;
      if (!(target instanceof Node)) return;
      if (readerControlsRef.current?.contains(target)) return;
      if (readerToggleRef.current?.contains(target)) return;
      setReaderControlsOpen(false);
    };

    document.addEventListener('pointerdown', handlePointerDown);
    return () => document.removeEventListener('pointerdown', handlePointerDown);
  }, [readerControlsOpen]);

  const getSelectedArticleLink = () => {
    const article = state.currentArticlePayload?.article ||
      state.articles.find((a) => a.id === state.selectedArticleId);
    return article?.source_url || (article as unknown as Record<string, string>)?.link || '';
  };

  return (
    <section id="view-articles" className="view is-active">
      <div className={`article-layout${listCollapsed ? ' is-collapsed' : ''}${state.mobileReading ? ' is-mobile-reading' : ''}`}>
        <div className="panel article-list">
          <div className="panel-header article-sidebar-header">
            <div>
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
                onClick={() => { void resetFilters(); }}
              >
                {t('articles.resetFilters', 'Reset Filters')}
              </button>
            </div>
          </div>
          <ArticleFilters
            filters={filters}
            groupOptions={groupOptions}
            accountOptions={accountOptions}
            total={Number(state.lastFacetPayload?.total || 0)}
            onChange={updateFilters}
          />
          <LoadingIndicator isLoading={state.isArticleLoading} id="article-search-loading" />
          <ArticleList
            onSelect={selectArticle}
            onLoadMore={() => loadArticles({ ...filters, search: deferredSearch }, false)}
          />
        </div>

        <ArticleResizer />

        <div className="panel article-preview">
          <div className="panel-header article-preview-topbar">
            <div>
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
                const content = previewRef.current?.querySelector('.reader');
                if (content) {
                  const text = (content as HTMLElement).innerText;
                  try { await copyToClipboard(text); showToast(t('articles.copied', '已复制全文')); } catch { showToast(t('articles.copyFailed', '复制失败')); }
                }
              }}>
                <span className="icon"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M16 1H4c-1.1 0-2 .9-2 2v14h2V3h12V1zm3 4H8c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h11c1.1 0 2-.9 2-2V7c0-1.1-.9-2-2-2zm0 16H8V7h11v14z"/></svg></span>
              </button>
              <button
                className="icon-btn"
                id="reader-toggle"
                type="button"
                ref={readerToggleRef}
                aria-label="Typography settings"
                title="Typography settings"
                onClick={() => setReaderControlsOpen((prev) => !prev)}
              >
                <span className="icon"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 6h16v2H4zM4 11h16v2H4zM4 16h16v2H4z"/></svg></span>
              </button>
              <button
                className="icon-btn"
                id="btn-article-toggle"
                type="button"
                aria-label="Toggle list"
                title="Toggle list"
                onClick={() => {
                  if (isNarrowViewport() && state.mobileReading) {
                    dispatch({ type: 'SET_MOBILE_READING', reading: false });
                    return;
                  }
                  setListCollapsed((prev) => !prev);
                }}
              >
                <span className="icon"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 6h16v2H4zM4 11h10v2H4zM4 16h16v2H4z"/></svg></span>
              </button>
            </div>
          </div>
          <div
            className={`reader-controls${readerControlsOpen ? ' is-open' : ''}`}
            id="reader-controls"
            ref={readerControlsRef}
          >
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
            <div className="reader-controls-section">
              <label className="switch">
                <input
                  id="reader-hide-small-images"
                  type="checkbox"
                  checked={config.hideSmall}
                  onChange={(event) => updateConfig({ hideSmall: event.target.checked })}
                />
                <span>{t('reader.hideSmallImages', 'Hide small images')}</span>
              </label>
              <p className="muted reader-controls-note">
                {t('reader.hideSmallImagesHint', 'Use local heuristics to reduce decorative or low-value images while reading.')}
              </p>
            </div>
          </div>
          <ArticlePreview previewRef={previewRef} />
        </div>
      </div>
    </section>
  );
}
