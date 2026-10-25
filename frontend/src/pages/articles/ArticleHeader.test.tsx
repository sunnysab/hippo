import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { I18nProvider } from '../../i18n';
import type { Article } from '../../store/articles';
import { ArticleHeader } from './ArticleHeader';

const article = (overrides: Partial<Article> = {}): Article => ({
  id: 1,
  biz: 'b1',
  article_id: 'a1',
  title: '标题',
  item_show_type: 0,
  author: '作者',
  digest: '摘要',
  cover: '',
  link: 'https://example.com',
  source_url: '',
  publish_at: 1792916311,
  created_at: '2026-10-25T00:00:00Z',
  account_nickname: '某号',
  account_alias: '',
  account_avatar: '',
  account_avatar_url: '',
  group_id: 1,
  group_name: '分组',
  image_id: null,
  source: 'weixin',
  read_at: null,
  starred_at: null,
  ...overrides,
});

const renderWith = (value: Article, onOpenEvent?: (id: number) => void) => render(
  <I18nProvider>
    <ArticleHeader article={value} onOpenEvent={onOpenEvent} />
  </I18nProvider>,
);

describe('ArticleHeader directions', () => {
  it('shows nothing before the article has been classified', () => {
    const { container } = renderWith(article({ topics: [] }));
    expect(container.querySelector('.article-preview-topics')).toBeNull();
  });

  it('lists each direction as a chip', () => {
    renderWith(article({ topics: [{ slug: 'ai', name: '人工智能' }, { slug: 'news', name: '新闻与时事' }] }));
    expect(screen.getByText('人工智能')).toBeTruthy();
    expect(screen.getByText('新闻与时事')).toBeTruthy();
  });
});

describe('ArticleHeader event', () => {
  it('stays hidden when the story has no second publisher', () => {
    const { container } = renderWith(article({ event: null }));
    expect(container.querySelector('.article-event')).toBeNull();
  });

  it('shows the story and how many reports it has', () => {
    renderWith(article({ event: { id: 7, title: '某事发生', article_count: 4, source_count: 3 } }));
    expect(screen.getByText('某事发生')).toBeTruthy();
    expect(screen.getByText('这件事共 4 篇报道')).toBeTruthy();
  });

  it('offers no jump when the caller cannot filter the list', () => {
    renderWith(article({ event: { id: 7, title: '某事发生', article_count: 4, source_count: 3 } }));
    expect(screen.queryByText('看这件事的其它报道')).toBeNull();
  });

  it('jumps to the story by id', () => {
    const onOpenEvent = vi.fn();
    renderWith(article({ event: { id: 7, title: '某事发生', article_count: 4, source_count: 3 } }), onOpenEvent);
    fireEvent.click(screen.getByText('看这件事的其它报道'));
    expect(onOpenEvent).toHaveBeenCalledWith(7);
  });
});
