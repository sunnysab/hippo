import { useDeferredValue, useEffect, useCallback, useRef, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { useArticlesState } from '../store/articles';
import { apiGet, apiSend, isAuthError } from '../api';
import { useToast } from './useToast';
import { useI18n } from '../i18n';
import { ARTICLE_SORT_PUBLISH_AT_DESC } from '../utils/constants';
import { onRefresh } from '../utils/events';
import {
  areArticleFiltersEqual,
  buildArticleRouteStateFromSearchParams,
  buildArticleSearchParams,
  type ArticleFiltersState,
  type ArticleSelectOption,
} from '../pages/articles/filtering';
import type { Article, ArticlePayload } from '../store/articles';

export function useArticleFilters() {
  const { state, dispatch } = useArticlesState();
  const { showToast } = useToast();
  const { t } = useI18n();
  const [searchParams, setSearchParams] = useSearchParams();
  const routeState = buildArticleRouteStateFromSearchParams(searchParams);
  const {
    groupId: routeGroupId,
    accountBiz: routeAccountBiz,
    itemShowType: routeItemShowType,
    source: routeSource,
    read: routeRead,
    starred: routeStarred,
    search: routeSearch,
    sort: routeSort,
  } = routeState.filters;

  const [filters, setFilters] = useState<ArticleFiltersState>(() => routeState.filters);
  const [articleIdParam, setArticleIdParam] = useState(routeState.articleId);
  const [itemIdParam, setItemIdParam] = useState(routeState.itemId);
  const [groupOptions, setGroupOptions] = useState<ArticleSelectOption[]>([]);
  const [accountOptions, setAccountOptions] = useState<ArticleSelectOption[]>([]);

  const deferredSearch = useDeferredValue(filters.search.trim());

  // Opening an article is reading it. Failure stays quiet: the read flag is
  // bookkeeping, not something worth interrupting a reader for.
  const markArticleRead = useCallback(async (id: number) => {
    try {
      const result = await apiSend(`/api/article/${id}/read`, 'PUT', { read: true }) as {
        read_at?: string | null;
      };
      dispatch({ type: 'SET_ARTICLE_READ', id, readAt: result.read_at ?? new Date().toISOString() });
    } catch {
      /* ignore */
    }
  }, [dispatch]);

  const isArticleLoadingRef = useRef(state.isArticleLoading);
  const listRequestRef = useRef(0);
  const listFiltersRef = useRef(filters);
  const accountRequestRef = useRef(0);
  const articlePageRef = useRef(state.articlePage);
  const articlePageSizeRef = useRef(state.articlePageSize);
  const articlesRef = useRef(state.articles);
  const filtersRef = useRef(filters);
  const articleIdParamRef = useRef(articleIdParam);
  const itemIdParamRef = useRef(itemIdParam);
  const deferredSearchRef = useRef(deferredSearch);
  const skipNextArticleTargetResolveRef = useRef(false);
  const pendingRouteTargetRef = useRef<{ articleId: string; itemId: string } | null>(null);
  const articleTargetRequestRef = useRef(0);
  const loadArticleDetail = useCallback(async (id: number, requestId: number) => {
    try {
      const payload = await apiGet(`/api/article/${id}`) as unknown as ArticlePayload;
      if (requestId !== articleTargetRequestRef.current) return;
      dispatch({ type: 'SELECT_ARTICLE', id, payload });
      dispatch({ type: 'SET_MOBILE_READING', reading: true });
      void markArticleRead(id);
    } catch (error) {
      if (requestId === articleTargetRequestRef.current && !isAuthError(error)) {
        showToast(error instanceof Error ? error.message : t('articles.loadFailed', 'Failed to load article.'));
      }
    }
  }, [dispatch, markArticleRead, showToast, t]);

  useEffect(() => () => {
    listRequestRef.current += 1;
    accountRequestRef.current += 1;
    articleTargetRequestRef.current += 1;
  }, []);

  useEffect(() => {
    isArticleLoadingRef.current = state.isArticleLoading;
    articlePageRef.current = state.articlePage;
    articlePageSizeRef.current = state.articlePageSize;
    articlesRef.current = state.articles;
    filtersRef.current = filters;
    articleIdParamRef.current = articleIdParam;
    itemIdParamRef.current = itemIdParam;
    deferredSearchRef.current = deferredSearch;
  }, [
    articleIdParam,
    deferredSearch,
    filters,
    state.articlePage,
    state.articlePageSize,
    state.articles,
    state.isArticleLoading,
    itemIdParam,
  ]);

  const loadArticles = useCallback(async (
    nextFilters: ArticleFiltersState,
    reset = true,
  ) => {
    if (!reset && isArticleLoadingRef.current) return;
    const requestId = ++listRequestRef.current;
    if (reset && !areArticleFiltersEqual(listFiltersRef.current, nextFilters)) {
      articlesRef.current = [];
      dispatch({ type: 'SET_ARTICLES', articles: [], reset: true });
      dispatch({ type: 'SET_FACET_PAYLOAD', payload: null });
    }
    listFiltersRef.current = nextFilters;
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
      if (nextFilters.source) url.searchParams.set('source', nextFilters.source);
      if (nextFilters.read) url.searchParams.set('read', nextFilters.read);
      if (nextFilters.starred) url.searchParams.set('starred', nextFilters.starred);
      if (nextFilters.search) url.searchParams.set('q', nextFilters.search);
      url.searchParams.set('sort', sort);
      url.searchParams.set('page', String(page));
      url.searchParams.set('page_size', String(pageSize));

      const payload = await apiGet(url.pathname + url.search);
      if (requestId !== listRequestRef.current) return;
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
    } catch (error) {
      if (requestId === listRequestRef.current && !isAuthError(error)) {
        showToast(error instanceof Error ? error.message : t('articles.loadMoreFailed', 'Failed to load articles.'));
      }
    } finally {
      if (requestId === listRequestRef.current) {
        isArticleLoadingRef.current = false;
        dispatch({ type: 'SET_LOADING', loading: false });
      }
    }
  }, [dispatch, showToast, t]);

  const syncUrlParams = useCallback((nextFilters: ArticleFiltersState) => {
    setSearchParams(buildArticleSearchParams(nextFilters, {
      articleId: articleIdParam,
      itemId: itemIdParam,
    }), { replace: true });
  }, [articleIdParam, setSearchParams, itemIdParam]);

  const updateFilters = useCallback((patch: Partial<ArticleFiltersState>) => {
    if ((patch.groupId !== undefined && patch.groupId !== filtersRef.current.groupId)
      || (patch.source !== undefined && patch.source !== filtersRef.current.source)) {
      accountRequestRef.current += 1;
      setAccountOptions([]);
    }
    articleTargetRequestRef.current += 1;
    pendingRouteTargetRef.current = { articleId: '', itemId: '' };
    setArticleIdParam('');
    setItemIdParam('');
    dispatch({ type: 'SET_MOBILE_READING', reading: false });
    dispatch({ type: 'SELECT_ARTICLE', id: null, payload: null });
    setFilters((prev) => {
      const groupChanged = patch.groupId !== undefined && patch.groupId !== prev.groupId;
      const sourceChanged = patch.source !== undefined && patch.source !== prev.source;
      return {
        ...prev,
        ...patch,
        ...(groupChanged || sourceChanged ? { accountBiz: '' } : null),
      };
    });
  }, [dispatch]);

  // 分组行上的数字回答的是“这个来源下这个组有多少号”，所以来源是它的一部分。
  const loadGroupOptions = useCallback(async (source: string) => {
    try {
      const payload = await apiGet(source ? `/api/group?source=${encodeURIComponent(source)}` : '/api/group');
      // 快切来源时旧答案可能后到：它数的是别的来源，写进去就是错的数字。
      if (filtersRef.current.source !== source) return;
      const groups = (payload.groups || []) as Array<{ id: number; name: string; account_count: number }>;
      const options = groups.map((group) => ({
        value: String(group.id),
        label: group.name,
        count: group.account_count,
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
    } catch (error) {
      if (filtersRef.current.source === source && !isAuthError(error)) {
        showToast(error instanceof Error ? error.message : t('articles.filtersFailed', 'Failed to load filters.'));
      }
    }
  }, [showToast, t]);

  const loadAccountOptions = useCallback(async (groupId: string, source: string) => {
    if (!groupId) return;

    const requestId = ++accountRequestRef.current;
    try {
      const payload = await apiGet(`/api/account?group_id=${encodeURIComponent(groupId)}&page_size=200${source ? `&source=${encodeURIComponent(source)}` : ''}`);
      if (requestId !== accountRequestRef.current || filtersRef.current.groupId !== groupId || filtersRef.current.source !== source) return;
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
    } catch (error) {
      if (requestId === accountRequestRef.current && !isAuthError(error)) {
        showToast(error instanceof Error ? error.message : t('articles.filtersFailed', 'Failed to load filters.'));
      }
    }
  }, [showToast, t]);

  const selectArticle = useCallback(async (id: number) => {
    skipNextArticleTargetResolveRef.current = true;
    pendingRouteTargetRef.current = { articleId: String(id), itemId: '' };
    setArticleIdParam(String(id));
    setItemIdParam('');
    dispatch({ type: 'SELECT_ARTICLE', id, payload: null });
    await loadArticleDetail(id, ++articleTargetRequestRef.current);
  }, [dispatch, loadArticleDetail]);

  const resetFilters = useCallback(async () => {
    setFilters({
      groupId: '',
      accountBiz: '',
      itemShowType: '',
      source: '',
      read: '',
      starred: '',
      search: '',
      sort: ARTICLE_SORT_PUBLISH_AT_DESC,
    });
    setAccountOptions([]);
    articleTargetRequestRef.current += 1;
    pendingRouteTargetRef.current = { articleId: '', itemId: '' };
    setArticleIdParam('');
    setItemIdParam('');
    dispatch({ type: 'SET_MOBILE_READING', reading: false });
    dispatch({ type: 'SELECT_ARTICLE', id: null, payload: null });
  }, [dispatch]);

  const resolveArticleTarget = useCallback(async () => {
    if (skipNextArticleTargetResolveRef.current) {
      skipNextArticleTargetResolveRef.current = false;
      return;
    }

    const requestId = ++articleTargetRequestRef.current;
    const articleId = articleIdParam.trim();
    const itemId = itemIdParam.trim();

    if (articleId) {
      if (!/^\d+$/.test(articleId)) {
        dispatch({ type: 'SELECT_ARTICLE', id: null, payload: null });
        return;
      }
      const numericArticleId = Number(articleId);
      dispatch({ type: 'SELECT_ARTICLE', id: numericArticleId, payload: null });
      await loadArticleDetail(numericArticleId, requestId);
      return;
    }

    if (!itemId) {
      dispatch({ type: 'SELECT_ARTICLE', id: null, payload: null });
      return;
    }

    const url = new URL('/api/article', window.location.origin);
    url.searchParams.set('article_id', itemId);
    url.searchParams.set('page', '1');
    url.searchParams.set('page_size', '1');
    url.searchParams.set('sort', ARTICLE_SORT_PUBLISH_AT_DESC);
    const payload = await apiGet(url.pathname + url.search);
    if (requestId !== articleTargetRequestRef.current) return;
    const article = ((payload.articles || []) as Article[])[0];
    if (!article) {
      dispatch({ type: 'SELECT_ARTICLE', id: null, payload: null });
      return;
    }

    dispatch({ type: 'SELECT_ARTICLE', id: article.id, payload: null });
    await loadArticleDetail(article.id, requestId);
  }, [articleIdParam, dispatch, itemIdParam, loadArticleDetail]);

  useEffect(() => {
    const timer = window.setTimeout(() => {
      const pendingRouteTarget = pendingRouteTargetRef.current;
      if (pendingRouteTarget) {
        const routeMatchesPending = (
          routeState.articleId === pendingRouteTarget.articleId &&
          routeState.itemId === pendingRouteTarget.itemId
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
        source: routeSource,
        read: routeRead,
        starred: routeStarred,
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
      if (itemIdParamRef.current !== routeState.itemId) {
        setItemIdParam(routeState.itemId);
      }
    }, 0);
    return () => window.clearTimeout(timer);
  }, [
    routeAccountBiz,
    routeState.articleId,
    routeGroupId,
    routeItemShowType,
    routeSource,
    routeRead,
    routeStarred,
    routeSearch,
    routeSort,
    routeState.itemId,
  ]);

  useEffect(() => {
    queueMicrotask(() => {
      void loadGroupOptions(filters.source);
    });
  }, [filters.source, loadGroupOptions]);

  useEffect(() => {
    if (!filters.groupId) return;
    queueMicrotask(() => {
      void loadAccountOptions(filters.groupId, filters.source);
    });
  }, [filters.groupId, filters.source, loadAccountOptions]);

  useEffect(() => {
    syncUrlParams(filters);
  }, [filters, syncUrlParams]);

  useEffect(() => {
    const nextFilters: ArticleFiltersState = {
      groupId: filters.groupId,
      accountBiz: filters.accountBiz,
      itemShowType: filters.itemShowType,
      source: filters.source,
      read: filters.read,
      starred: filters.starred,
      search: deferredSearch,
      sort: filters.sort,
    };
    void loadArticles(nextFilters, true);
  }, [
    deferredSearch,
    filters.accountBiz,
    filters.groupId,
    filters.itemShowType,
    filters.source,
    filters.read,
    filters.starred,
    filters.sort,
    loadArticles,
  ]);

  useEffect(() => {
    void resolveArticleTarget();
  }, [resolveArticleTarget]);

  useEffect(() => {
    const handler = () => {
      const nextFilters = filtersRef.current;
      const nextSearch = deferredSearchRef.current;
      void loadGroupOptions(nextFilters.source);
      if (nextFilters.groupId) {
        void loadAccountOptions(nextFilters.groupId, nextFilters.source);
      }
      void loadArticles({ ...nextFilters, search: nextSearch }, true);
      void resolveArticleTarget();
    };
    return onRefresh(handler);
  }, [loadAccountOptions, loadArticles, loadGroupOptions, resolveArticleTarget]);

  return {
    filters,
    groupOptions,
    accountOptions,
    deferredSearch,
    updateFilters,
    resetFilters,
    selectArticle,
    loadArticles,
  };
}
