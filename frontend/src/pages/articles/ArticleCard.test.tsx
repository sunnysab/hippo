import { fireEvent, render } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import type { Article } from '../../store/articles';
import { ArticleCard } from './ArticleCard';

const article: Article = {
  id: 1,
  biz: 'Mz123',
  article_id: 'a1',
  title: '一张封面加载失败的文章',
  item_show_type: null,
  author: '',
  digest: '',
  cover: '',
  link: '',
  source_url: '',
  publish_at: 0,
  created_at: '',
  account_nickname: '中投数研',
  account_alias: '',
  account_avatar: '',
  account_avatar_url: '/api/account/Mz123/avatar',
  group_id: 1,
  group_name: '',
  image_id: 9,
  source: 'weixin',
  read_at: null,
};

describe('ArticleCard', () => {
  it('keeps the thumbnail column when the cover fails to load', () => {
    const { container } = render(
      <ArticleCard article={article} isActive={false} onClick={vi.fn()} onContextMenu={vi.fn()} />,
    );

    fireEvent.error(container.querySelector('img.article-thumb')!);

    // 摘掉封面元素会让 grid 把正文挤进封面列，整张卡片跟着错位。
    const thumb = container.querySelector('.article-thumb')!;
    expect(thumb.tagName).toBe('DIV');
    expect(thumb.className).toContain('placeholder');
  });

  it('marks an unread article with a dot and no read class', () => {
    const { container } = render(
      <ArticleCard article={article} isActive={false} onClick={vi.fn()} onContextMenu={vi.fn()} />,
    );

    expect(container.querySelector('.article-unread-dot')).not.toBeNull();
    expect(container.querySelector('.article-card')!.className).not.toContain('is-read');
  });

  it('drops the dot once the article has been read', () => {
    const { container } = render(
      <ArticleCard
        article={{ ...article, read_at: '2026-05-02T11:00:00+00:00' }}
        isActive={false}
        onClick={vi.fn()}
        onContextMenu={vi.fn()}
      />,
    );

    expect(container.querySelector('.article-unread-dot')).toBeNull();
    expect(container.querySelector('.article-card')!.className).toContain('is-read');
  });

  it('toggles read state without opening the article', () => {
    const onToggleRead = vi.fn();
    const onClick = vi.fn();
    const { container } = render(
      <ArticleCard
        article={article}
        isActive={false}
        onClick={onClick}
        onContextMenu={vi.fn()}
        onToggleRead={onToggleRead}
      />,
    );

    fireEvent.click(container.querySelector('.article-card-action')!);

    expect(onToggleRead).toHaveBeenCalledWith(article);
    // The row's own click must not also fire, or the reader jumps to the article.
    expect(onClick).not.toHaveBeenCalled();
  });
});
