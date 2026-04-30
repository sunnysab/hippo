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

export interface ArticleSelectOption {
  value: string;
  label: string;
}

export const getDefaultArticleSort = (search: string): string => (
  search.trim() ? ARTICLE_SORT_RELEVANCE_DESC : ARTICLE_SORT_PUBLISH_AT_DESC
);

export const buildArticleFiltersFromSearchParams = (
  searchParams: URLSearchParams,
): ArticleFiltersState => {
  const search = searchParams.get('q') || '';
  return {
    groupId: searchParams.get('group') || '',
    accountBiz: searchParams.get('account') || '',
    itemShowType: searchParams.get('type') || '',
    search,
    sort: searchParams.get('sort') || getDefaultArticleSort(search),
  };
};

export const buildArticleSearchParams = (
  filters: ArticleFiltersState,
): URLSearchParams => {
  const params = new URLSearchParams();
  if (filters.groupId) params.set('group', filters.groupId);
  if (filters.accountBiz) params.set('account', filters.accountBiz);
  if (filters.itemShowType) params.set('type', filters.itemShowType);
  if (filters.search.trim()) params.set('q', filters.search.trim());

  const defaultSort = getDefaultArticleSort(filters.search);
  if (filters.sort && filters.sort !== defaultSort) {
    params.set('sort', filters.sort);
  }

  return params;
};
