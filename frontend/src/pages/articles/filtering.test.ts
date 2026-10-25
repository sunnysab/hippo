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
  topic: '',
  eventId: '',
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

describe('topic filter', () => {
  it('round-trips through the URL so a filter is shareable', () => {
    const params = buildArticleSearchParams(filters({ topic: 'ai,news' }));

    expect(params.get('topic')).toBe('ai,news');
    expect(buildArticleRouteStateFromSearchParams(params).filters.topic).toBe('ai,news');
  });

  it('stays out of the URL when no direction is selected', () => {
    expect(buildArticleSearchParams(filters()).get('topic')).toBeNull();
    expect(buildArticleRouteStateFromSearchParams(new URLSearchParams()).filters.topic).toBe('');
  });

  it('counts as a difference, so the list actually reloads', () => {
    expect(areArticleFiltersEqual(filters(), filters({ topic: 'ai' }))).toBe(false);
    expect(areArticleFiltersEqual(filters({ topic: 'ai' }), filters({ topic: 'ai' }))).toBe(true);
    // Order is part of the value: the facet component writes vocabulary order.
    expect(areArticleFiltersEqual(filters({ topic: 'ai,news' }), filters({ topic: 'news,ai' }))).toBe(false);
  });

  it('coexists with the layout type filter', () => {
    const params = buildArticleSearchParams(filters({ itemShowType: '5', topic: 'ai' }));

    expect(params.get('type')).toBe('5');
    expect(params.get('topic')).toBe('ai');
  });
});

describe('event filter', () => {
  it('round-trips through the URL as event_id', () => {
    const params = buildArticleSearchParams(filters({ eventId: '12' }));

    expect(params.get('event')).toBe('12');
    expect(buildArticleRouteStateFromSearchParams(params).filters.eventId).toBe('12');
  });

  it('stays out of the URL when every story is shown', () => {
    expect(buildArticleSearchParams(filters()).get('event')).toBeNull();
  });

  it('counts as a difference, so switching stories reloads', () => {
    expect(areArticleFiltersEqual(filters(), filters({ eventId: '12' }))).toBe(false);
  });
});
