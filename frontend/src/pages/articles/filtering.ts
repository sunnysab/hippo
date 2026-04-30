import {
  ARTICLE_SORT_PUBLISH_AT_DESC,
  ARTICLE_SORT_RELEVANCE_DESC,
} from '../../utils/constants';

export interface ArticleFiltersState {
  groupId: string;
  accountBiz: string;
  itemShowType: string;
  search: string;
  sort: string;
}

export interface ArticleRouteState {
  filters: ArticleFiltersState;
  articleId: string;
  wxArticleId: string;
}

export interface ArticleSelectOption {
  value: string;
  label: string;
}

export const areArticleFiltersEqual = (
  left: ArticleFiltersState,
  right: ArticleFiltersState,
): boolean => (
  left.groupId === right.groupId &&
  left.accountBiz === right.accountBiz &&
  left.itemShowType === right.itemShowType &&
  left.search === right.search &&
  left.sort === right.sort
);

export const getDefaultArticleSort = (search: string): string => (
  search.trim() ? ARTICLE_SORT_RELEVANCE_DESC : ARTICLE_SORT_PUBLISH_AT_DESC
);

export const buildArticleRouteStateFromSearchParams = (
  searchParams: URLSearchParams,
): ArticleRouteState => {
  const search = searchParams.get('q') || '';
  return {
    filters: {
      groupId: searchParams.get('group') || '',
      accountBiz: searchParams.get('account') || '',
      itemShowType: searchParams.get('type') || '',
      search,
      sort: searchParams.get('sort') || getDefaultArticleSort(search),
    },
    articleId: searchParams.get('article') || '',
    wxArticleId: searchParams.get('wx_article') || '',
  };
};

export const buildArticleSearchParams = (
  filters: ArticleFiltersState,
  routeTarget?: { articleId?: string; wxArticleId?: string },
): URLSearchParams => {
  const params = new URLSearchParams();
  if (filters.groupId) params.set('group', filters.groupId);
  if (filters.accountBiz) params.set('account', filters.accountBiz);
  if (filters.itemShowType) params.set('type', filters.itemShowType);
  if (filters.search.trim()) params.set('q', filters.search.trim());
  if (routeTarget?.articleId) params.set('article', routeTarget.articleId);
  if (routeTarget?.wxArticleId) params.set('wx_article', routeTarget.wxArticleId);

  const defaultSort = getDefaultArticleSort(filters.search);
  if (filters.sort && filters.sort !== defaultSort) {
    params.set('sort', filters.sort);
  }

  return params;
};
