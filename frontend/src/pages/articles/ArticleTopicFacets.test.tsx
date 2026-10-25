import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { ArticlesProvider, useArticlesState } from '../../store/articles';
import { I18nProvider } from '../../i18n';
import { useEffect } from 'react';
import { ArticleTopicFacets } from './ArticleTopicFacets';
import type { FacetPayload } from '../../store/articles';

const payload: FacetPayload = {
  total: 10,
  item_show_type_facets: [],
  topic_facets: [
    { slug: 'ai', name: '人工智能', count: 6 },
    { slug: 'news', name: '新闻与时事', count: 4 },
  ],
  // Deliberately not 6 + 4: one article may carry two directions, so counting once
  // per article gives a smaller number than summing the bars.
  classified_total: 8,
};

/** Seeds the shared facet payload the component reads, then renders it. */
function Seeded({ activeTopic, onChange }: { activeTopic: string; onChange: (topic: string) => void }) {
  const { dispatch } = useArticlesState();
  useEffect(() => {
    dispatch({ type: 'SET_FACET_PAYLOAD', payload });
  }, [dispatch]);
  return <ArticleTopicFacets activeTopic={activeTopic} onChange={onChange} />;
}

const renderWith = (activeTopic: string, onChange = vi.fn()) => {
  render(
    <I18nProvider>
      <ArticlesProvider>
        <Seeded activeTopic={activeTopic} onChange={onChange} />
      </ArticlesProvider>
    </I18nProvider>,
  );
  return onChange;
};

describe('ArticleTopicFacets', () => {
  it('shows one bar per vocabulary entry with its own count', () => {
    renderWith('');
    expect(screen.getByText('人工智能')).toBeTruthy();
    expect(screen.getByText('新闻与时事')).toBeTruthy();
    expect(screen.getByText('6')).toBeTruthy();
    expect(screen.getByText('4')).toBeTruthy();
  });

  it('shows the all-directions count as articles, not the sum of the bars', () => {
    renderWith('');
    const all = screen.getByText('全部方向').closest('button')!;
    // 8, not 6 + 4 = 10: an article with two directions is still one article.
    expect(all.textContent).toContain('8');
    expect(all.textContent).not.toContain('10');
  });

  it('renders nothing before a facet payload arrives', () => {
    const { container } = render(
      <I18nProvider>
        <ArticlesProvider>
          <ArticleTopicFacets activeTopic="" onChange={vi.fn()} />
        </ArticlesProvider>
      </I18nProvider>,
    );
    expect(container.querySelector('#article-topic-facets')).toBeNull();
  });

  it('selects a direction and writes vocabulary order into the value', () => {
    const onChange = renderWith('');
    fireEvent.click(screen.getByText('新闻与时事'));
    expect(onChange).toHaveBeenCalledWith('news');
  });

  it('keeps the selection in vocabulary order when two are picked', () => {
    const onChange = renderWith('news');
    // Clicking "ai" while "news" is active must not produce "ai,news" by click order.
    fireEvent.click(screen.getByText('人工智能'));
    expect(onChange).toHaveBeenCalledWith('ai,news');
  });

  it('toggles a direction off', () => {
    const onChange = renderWith('ai,news');
    fireEvent.click(screen.getByText('人工智能'));
    expect(onChange).toHaveBeenCalledWith('news');
  });

  it('clears every direction from the all bar', () => {
    const onChange = renderWith('ai');
    fireEvent.click(screen.getByText('全部方向'));
    expect(onChange).toHaveBeenCalledWith('');
  });

  it('marks the active directions for assistive technology, not just by colour', () => {
    renderWith('ai');
    expect(screen.getByText('人工智能').closest('button')!.getAttribute('aria-pressed')).toBe('true');
    expect(screen.getByText('新闻与时事').closest('button')!.getAttribute('aria-pressed')).toBe('false');
  });
});
