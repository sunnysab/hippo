import { fireEvent, render } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ArticleResizer } from './ArticleResizer';

describe('ArticleResizer', () => {
  beforeEach(() => {
    vi.stubGlobal('localStorage', { getItem: () => null, setItem: vi.fn() });
    vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function (this: HTMLElement) {
      const width = this.classList.contains('article-layout') ? 1200
        : this.classList.contains('article-view-rail') ? 200 : 400;
      return { width, height: 800, x: 0, y: 0, top: 0, left: 0, right: width, bottom: 800, toJSON() {} };
    });
  });

  afterEach(() => {
    document.documentElement.style.removeProperty('--article-list-width');
    vi.unstubAllGlobals();
  });

  it.each([false, true])('leaves room for the reading pane when dragging (wide rail: %s)', (rail) => {
    const { container } = render(<div className="article-layout">
      {rail ? <aside className="article-view-rail" /> : null}
      <div className="article-list" /><ArticleResizer />
    </div>);
    fireEvent.mouseDown(container.querySelector('.article-resizer')!, { clientX: 400 });
    fireEvent.mouseMove(document, { clientX: 1100 });
    const width = document.documentElement.style.getPropertyValue('--article-list-width');
    expect(width).toBe(rail ? '478px' : '690px');
    fireEvent.mouseUp(document);
    expect(localStorage.setItem).toHaveBeenCalledWith('hippo-article-width', width);
  });
});
