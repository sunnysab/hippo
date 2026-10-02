import { useRef, useCallback, useEffect } from 'react';

// Kept in sync with the list column's `minmax(360px, …)` in articles.css.
const MIN_LIST_WIDTH = 360;
const MAX_LIST_WIDTH = 800;

const clampWidth = (value: number) =>
  Math.min(Math.max(value, MIN_LIST_WIDTH), MAX_LIST_WIDTH);

export function ArticleResizer() {
  const resizerRef = useRef<HTMLDivElement>(null);
  const root = document.documentElement;

  const startDrag = useCallback((e: React.MouseEvent | React.TouchEvent) => {
    const isTouch = 'touches' in e;
    const startX = isTouch ? e.touches[0].clientX : e.clientX;

    // Get current width from the list panel element
    const listPanel = document.querySelector('.article-list') as HTMLElement | null;
    let startWidth = MIN_LIST_WIDTH;
    if (listPanel) {
      const rect = listPanel.getBoundingClientRect();
      if (rect.width > 0) startWidth = rect.width;
    } else {
      const current = getComputedStyle(root).getPropertyValue('--article-list-width').trim();
      const parsed = parseFloat(current);
      if (Number.isFinite(parsed) && parsed > 0) startWidth = parsed;
    }

    const onMove = (ev: MouseEvent | TouchEvent) => {
      const clientX = 'touches' in ev ? ev.touches[0].clientX : ev.clientX;
      const delta = clientX - startX;
      root.style.setProperty('--article-list-width', `${clampWidth(startWidth + delta)}px`);
    };

    const onUp = () => {
      document.body.style.cursor = '';
      document.removeEventListener('mousemove', onMove);
      document.removeEventListener('mouseup', onUp);
      document.removeEventListener('touchmove', onMove);
      document.removeEventListener('touchend', onUp);
      const current = getComputedStyle(root).getPropertyValue('--article-list-width').trim();
      if (current) {
        localStorage.setItem('hippo-article-width', current);
      }
    };

    document.body.style.cursor = 'col-resize';
    document.addEventListener('mousemove', onMove);
    document.addEventListener('mouseup', onUp);
    document.addEventListener('touchmove', onMove);
    document.addEventListener('touchend', onUp);
  }, [root]);

  // Restore the saved width, clamped to the range the layout can actually use.
  useEffect(() => {
    const saved = parseFloat(localStorage.getItem('hippo-article-width') ?? '');
    if (Number.isFinite(saved)) {
      root.style.setProperty('--article-list-width', `${clampWidth(saved)}px`);
    }
  }, [root]);

  return (
    <div
      ref={resizerRef}
      className="article-resizer"
      id="article-resizer"
      role="separator"
      aria-orientation="vertical"
      onMouseDown={startDrag}
      onTouchStart={startDrag}
    />
  );
}
