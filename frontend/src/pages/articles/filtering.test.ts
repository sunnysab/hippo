import { describe, expect, it } from 'vitest';
import {
  areArticleFiltersEqual,
  buildArticleRouteStateFromSearchParams,
  buildArticleSearchParams,
  type ArticleFiltersState,
} from './filtering';

const filters = (overrides: Partial<ArticleFiltersState> = {}): ArticleFiltersState => ({
  groupId: '',
  accountBiz: '',
  itemShowType: '',
  source: '',
  read: '',
  starred: '',
  search: '',
  sort: 'publish_at_desc',
  ...overrides,
});

describe('source filter', () => {
  it('round-trips through the URL so a rail click is shareable', () => {
    const params = buildArticleSearchParams(filters({ source: 'bilibili' }));

    expect(params.get('source')).toBe('bilibili');
    expect(buildArticleRouteStateFromSearchParams(params).filters.source).toBe('bilibili');
  });

  it('stays out of the URL when every source is shown', () => {
    expect(buildArticleSearchParams(filters()).get('source')).toBeNull();
  });

  it('counts as a difference, so the list actually reloads', () => {
    expect(areArticleFiltersEqual(filters(), filters({ source: 'podcast' }))).toBe(false);
  });
});
