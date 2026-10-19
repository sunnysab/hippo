import { describe, expect, it } from 'vitest';
import { buildFilterSettingsPayload } from './form';

describe('buildFilterSettingsPayload', () => {
  it('sends only the personal reading key, trimmed', () => {
    expect(buildFilterSettingsPayload({ articleExcludeKeywords: '  promo\nad  ' })).toEqual({
      article_exclude_keywords: 'promo\nad',
    });
  });
});
