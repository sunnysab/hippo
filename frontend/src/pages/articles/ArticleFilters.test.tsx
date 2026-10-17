import { fireEvent, render } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { ArticlesProvider } from '../../store/articles';
import { I18nProvider } from '../../i18n';
import zhCN from '../../i18n/zh-CN.json';
import { ArticleFilters } from './ArticleFilters';
import type { ArticleFiltersState } from './filtering';

const filters: ArticleFiltersState = {
  groupId: '',
  accountBiz: '',
  itemShowType: '',
  source: '',
  read: '',
  starred: '',
  search: '',
  sort: '',
};

describe('ArticleFilters', () => {
  it('offers every account without calling it a WeChat official account', () => {
    const { container } = render(
      <I18nProvider>
        <ArticlesProvider>
          <ArticleFilters
            filters={filters}
            groupOptions={[]}
            accountOptions={[{ value: 'b1', label: '某B站UP' }]}
            onChange={vi.fn()}
            onReset={vi.fn()}
          />
        </ArticlesProvider>
      </I18nProvider>,
    );

    const select = container.querySelector('#article-account-filter')!;
    expect(select.textContent).toContain(zhCN['filters.allAccounts']);
    expect(select.textContent).toContain('某B站UP');
  });

  it('keeps the account selection behaviour unchanged', () => {
    const onChange = vi.fn();

    const { container } = render(
      <I18nProvider>
        <ArticlesProvider>
          <ArticleFilters
            filters={filters}
            groupOptions={[]}
            accountOptions={[{ value: 'b1', label: '某B站UP' }]}
            onChange={onChange}
            onReset={vi.fn()}
          />
        </ArticlesProvider>
      </I18nProvider>,
    );

    fireEvent.change(container.querySelector('#article-account-filter')!, { target: { value: 'b1' } });

    expect(onChange).toHaveBeenCalledWith({ accountBiz: 'b1' });
  });
});
