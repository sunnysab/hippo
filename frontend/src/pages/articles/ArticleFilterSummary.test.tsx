import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { I18nProvider } from '../../i18n';
import { ArticleFilterSummary } from './ArticleFilterSummary';
import type { ArticleFiltersState } from './filtering';

describe('ArticleFilterSummary', () => {
  it('decodes URL-encoded search keywords in the summary', () => {
    const filters: ArticleFiltersState = {
      groupId: 'g1',
      accountBiz: '',
      itemShowType: '',
      search: 'ai%20%E6%8A%95%E8%B5%84',
      sort: '',
    };

    render(
      <I18nProvider>
        <ArticleFilterSummary
          filters={filters}
          groupOptions={[{ value: 'g1', label: '经济&金融' }]}
          accountOptions={[]}
          total={12}
        />
      </I18nProvider>,
    );

    expect(screen.getByText('分组：经济&金融')).toBeTruthy();
    expect(screen.getByText('搜索：ai 投资')).toBeTruthy();
  });
});
