import {
  ARTICLE_SORT_PUBLISH_AT_DESC,
  ARTICLE_SORT_RELEVANCE_DESC,
} from '../../utils/constants';

export interface ArticleFiltersState {
  groupId: string;
  accountBiz: string;
  itemShowType: string;
  /** '' for every direction, otherwise a comma-separated slug list (ai,news). */
  topic: string;
  /** '' for every story, otherwise one event id: "the other reports of this". */
  eventId: string;
  /** '' for every source, otherwise `accounts.source` (weixin/bilibili/…). */
  source: string;
  /** '' for every article, `unread` for the unread box. */
  read: string;
  /** 'true' keeps only starred articles. */
  starred: string;
  /** The personal ordered queue, independent of feed filters. */
  watchLater?: boolean;
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
  /** Subscriptions behind the option, as the API counts them. */
  count?: number;
}

export const areArticleFiltersEqual = (
  left: ArticleFiltersState,
  right: ArticleFiltersState,
): boolean => (
  left.groupId === right.groupId &&
  left.accountBiz === right.accountBiz &&
  left.itemShowType === right.itemShowType &&
  left.topic === right.topic &&
  left.eventId === right.eventId &&
  left.source === right.source &&
  left.read === right.read &&
  left.starred === right.starred &&
  Boolean(left.watchLater) === Boolean(right.watchLater) &&
  left.search === right.search &&
  left.sort === right.sort
);

export const getDefaultArticleSort = (search: string): string => (
  search.trim() ? ARTICLE_SORT_RELEVANCE_DESC : ARTICLE_SORT_PUBLISH_AT_DESC
);

export const buildArticleRouteStateFromSearchParams = (
  searchParams: URLSearchParams,
): ArticleRouteState => {
  if (searchParams.get('view') === 'watch-later') {
    searchParams = new URLSearchParams({ view: 'watch-later', article: searchParams.get('article') || '' });
  }
  const search = searchParams.get('q') || '';
  return {
    filters: {
      groupId: searchParams.get('group') || '',
      accountBiz: searchParams.get('account') || '',
      itemShowType: searchParams.get('type') || '',
      topic: searchParams.get('topic') || '',
      eventId: searchParams.get('event') || '',
      source: searchParams.get('source') || '',
      read: searchParams.get('read') || '',
      starred: searchParams.get('starred') || '',
      ...(searchParams.get('view') === 'watch-later' ? { watchLater: true } : {}),
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
  if (filters.watchLater) {
    const params = new URLSearchParams({ view: 'watch-later' });
    if (routeTarget?.articleId) params.set('article', routeTarget.articleId);
    return params;
  }
  const params = new URLSearchParams();
  if (filters.groupId) params.set('group', filters.groupId);
  if (filters.accountBiz) params.set('account', filters.accountBiz);
  if (filters.itemShowType) params.set('type', filters.itemShowType);
  if (filters.topic) params.set('topic', filters.topic);
  if (filters.eventId) params.set('event', filters.eventId);
  if (filters.source) params.set('source', filters.source);
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
