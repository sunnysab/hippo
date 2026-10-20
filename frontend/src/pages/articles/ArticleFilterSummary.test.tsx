import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { I18nProvider } from '../../i18n';
import zhCN from '../../i18n/zh-CN.json';
import { ArticleFilterSummary } from './ArticleFilterSummary';
import type { ArticleFiltersState } from './filtering';

describe('ArticleFilterSummary', () => {
  it('decodes URL-encoded search keywords in the summary', () => {
    const filters: ArticleFiltersState = {
      groupId: 'g1',
      accountBiz: '',
      itemShowType: '',
      source: '',
      read: '',
      starred: '',
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

  it('calls a non-WeChat account an account, not a WeChat official one', () => {
    const filters: ArticleFiltersState = {
      groupId: '',
      accountBiz: 'b1',
      itemShowType: '',
      source: '',
      read: '',
      starred: '',
      search: '',
      sort: '',
    };

    render(
      <I18nProvider>
        <ArticleFilterSummary
          filters={filters}
          groupOptions={[]}
          accountOptions={[{ value: 'b1', label: '某B站UP' }]}
          total={3}
        />
      </I18nProvider>,
    );

    expect(screen.getByText(zhCN['articles.summary.account'].replace('{value}', '某B站UP'))).toBeTruthy();
    expect(screen.queryByText(/公众号/m)).toBeNull();
  });

  it('shows the source and an account constraint even when its label is not loaded', () => {
    render(<I18nProvider><ArticleFilterSummary
      filters={{ groupId: '', accountBiz: 'channel-id', itemShowType: '', source: 'youtube', read: '', starred: '', search: '', sort: '' }}
      groupOptions={[]} accountOptions={[]} total={0} />
    </I18nProvider>);
    expect(screen.getByText('YouTube')).toBeTruthy();
    expect(screen.getByText(zhCN['articles.summary.account'].replace('{value}', 'channel-id'))).toBeTruthy();
  });

});
