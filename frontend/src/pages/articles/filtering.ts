import {
  ARTICLE_SORT_PUBLISH_AT_DESC,
  ARTICLE_SORT_RELEVANCE_DESC,
} from '../../utils/constants';

export interface ArticleFiltersState {
  groupId: string;
  accountBiz: string;
  itemShowType: string;
  /** '' for every article, `unread` for the unread box. */
  read: string;
  /** 'true' keeps only starred articles. */
  starred: string;
  search: string;
  sort: string;
}

export interface ArticleRouteState {
  filters: ArticleFiltersState;
  articleId: string;
  itemId: string;
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
  left.read === right.read &&
  left.starred === right.starred &&
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
      read: searchParams.get('read') || '',
      starred: searchParams.get('starred') || '',
      search,
      sort: searchParams.get('sort') || getDefaultArticleSort(search),
    },
    articleId: searchParams.get('article') || '',
    itemId: searchParams.get('item_id') || '',
  };
};

export const buildArticleSearchParams = (
  filters: ArticleFiltersState,
  routeTarget?: { articleId?: string; itemId?: string },
): URLSearchParams => {
  const params = new URLSearchParams();
  if (filters.groupId) params.set('group', filters.groupId);
  if (filters.accountBiz) params.set('account', filters.accountBiz);
  if (filters.itemShowType) params.set('type', filters.itemShowType);
  if (filters.read) params.set('read', filters.read);
  if (filters.starred) params.set('starred', filters.starred);
  if (filters.search.trim()) params.set('q', filters.search.trim());
  if (routeTarget?.articleId) params.set('article', routeTarget.articleId);
  if (routeTarget?.itemId) params.set('item_id', routeTarget.itemId);

  const defaultSort = getDefaultArticleSort(filters.search);
  if (filters.sort && filters.sort !== defaultSort) {
    params.set('sort', filters.sort);
  }

  return params;
};
