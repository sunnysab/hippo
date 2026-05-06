import { useDeferredValue, useEffect, useCallback, useRef, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { useArticlesState } from '../store/articles';
import { useMediaQuery } from './useMediaQuery';
import { apiGet } from '../api';
import { ARTICLE_SORT_PUBLISH_AT_DESC } from '../utils/constants';
import { createLatestOnly } from '../utils/latest';
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
  const [searchParams, setSearchParams] = useSearchParams();
  const routeState = buildArticleRouteStateFromSearchParams(searchParams);
  const {
    groupId: routeGroupId,
    accountBiz: routeAccountBiz,
    itemShowType: routeItemShowType,
    search: routeSearch,
    sort: routeSort,
  } = routeState.filters;

  const [filters, setFilters] = useState<ArticleFiltersState>(() => routeState.filters);
  const [articleIdParam, setArticleIdParam] = useState(routeState.articleId);
  const [wxArticleIdParam, setWxArticleIdParam] = useState(routeState.wxArticleId);
  const [groupOptions, setGroupOptions] = useState<ArticleSelectOption[]>([]);
  const [accountOptions, setAccountOptions] = useState<ArticleSelectOption[]>([]);

  const deferredSearch = useDeferredValue(filters.search.trim());
  const isNarrowViewport = useMediaQuery('(max-width: 720px)');

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
  const latestArticleDetailRef = useRef(createLatestOnly<{
    id: number;
    payload: ArticlePayload;
    openMobileReader: boolean;
  }>(({ id, payload, openMobileReader }) => {
    dispatch({ type: 'SELECT_ARTICLE', id, payload });
    if (openMobileReader) {
      dispatch({ type: 'SET_MOBILE_READING', reading: true });
    }
  }));

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
    await latestArticleDetailRef.current(async () => ({
      id,
      payload: await apiGet(`/api/article/${id}`) as unknown as ArticlePayload,
      openMobileReader: isNarrowViewport,
    }));
  }, [dispatch, isNarrowViewport]);

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
      const numericArticleId = Number(articleId);
      dispatch({ type: 'SELECT_ARTICLE', id: numericArticleId, payload: null });
      await latestArticleDetailRef.current(async () => ({
        id: numericArticleId,
        payload: await apiGet(`/api/article/${articleId}`) as unknown as ArticlePayload,
        openMobileReader: isNarrowViewport,
      }));
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
    await latestArticleDetailRef.current(async () => ({
      id: article.id,
      payload: await apiGet(`/api/article/${article.id}`) as unknown as ArticlePayload,
      openMobileReader: isNarrowViewport,
    }));
  }, [articleIdParam, dispatch, isNarrowViewport, wxArticleIdParam]);

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
    queueMicrotask(() => {
      void loadGroupOptions();
    });
  }, [loadGroupOptions]);

  useEffect(() => {
    if (!filters.groupId) return;
    queueMicrotask(() => {
      void loadAccountOptions(filters.groupId);
    });
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
    void loadArticles(nextFilters, true);
  }, [
    deferredSearch,
    filters.accountBiz,
    filters.groupId,
    filters.itemShowType,
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
      void loadGroupOptions();
      if (nextFilters.groupId) {
        void loadAccountOptions(nextFilters.groupId);
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
