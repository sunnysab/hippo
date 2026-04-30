import { useRef, useCallback, useEffect } from 'react';

export function ArticleResizer() {
  const resizerRef = useRef<HTMLDivElement>(null);
  const root = document.documentElement;

  const startDrag = useCallback((e: React.MouseEvent | React.TouchEvent) => {
    const isTouch = 'touches' in e;
    const startX = isTouch ? e.touches[0].clientX : e.clientX;

    // Get current width from the list panel element
    const listPanel = document.querySelector('.article-list') as HTMLElement | null;
    let startWidth = 300;
    if (listPanel) {
      const rect = listPanel.getBoundingClientRect();
      if (rect.width > 0) startWidth = rect.width;
    } else {
      const current = getComputedStyle(root).getPropertyValue('--article-list-width').trim();
      const parsed = parseFloat(current);
      if (Number.isFinite(parsed) && parsed > 0) startWidth = parsed;
    }

    const clamp = (v: number, lo: number, hi: number) => Math.min(Math.max(v, lo), hi);

    const onMove = (ev: MouseEvent | TouchEvent) => {
      const clientX = 'touches' in ev ? ev.touches[0].clientX : ev.clientX;
      const delta = clientX - startX;
      const width = clamp(startWidth + delta, 220, 800);
      root.style.setProperty('--article-list-width', `${width}px`);
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

  // Load saved width
  useEffect(() => {
    const saved = localStorage.getItem('hippo-article-width');
    if (saved) {
      root.style.setProperty('--article-list-width', saved);
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
