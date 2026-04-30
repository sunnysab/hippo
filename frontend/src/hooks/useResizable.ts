import { useState, useRef, useCallback, useEffect } from 'react';

export function useResizable(options: {
  storageKey?: string;
  min?: number;
  max?: number;
  defaultWidth?: number;
}) {
  const { storageKey, min = 220, max = 800, defaultWidth = 360 } = options;

  const [width, setWidth] = useState(() => {
    if (storageKey) {
      try {
        const saved = localStorage.getItem(storageKey);
        if (saved) return saved;
      } catch { /* ignore */ }
    }
    return `${defaultWidth}px`;
  });

  useEffect(() => {
    const root = document.documentElement;
    root.style.setProperty('--article-list-width', width);
  }, [width]);

  const startDrag = useCallback(
    (e: React.MouseEvent | React.TouchEvent) => {
      const isTouch = 'touches' in e;
      const startX = isTouch ? e.touches[0].clientX : e.clientX;
      const startWidth = parseFloat(width) || defaultWidth;

      const clamp = (v: number, lo: number, hi: number) => Math.min(Math.max(v, lo), hi);

      const onMove = (ev: MouseEvent | TouchEvent) => {
        const clientX = 'touches' in ev ? ev.touches[0].clientX : ev.clientX;
        const delta = clientX - startX;
        const newWidth = clamp(startWidth + delta, min, max);
        document.documentElement.style.setProperty('--article-list-width', `${newWidth}px`);
      };

      const onUp = () => {
        document.body.style.cursor = '';
        document.removeEventListener('mousemove', onMove);
        document.removeEventListener('mouseup', onUp);
        document.removeEventListener('touchmove', onMove);
        document.removeEventListener('touchend', onUp);
        const current = getComputedStyle(document.documentElement)
          .getPropertyValue('--article-list-width')
          .trim();
        if (current && storageKey) {
          localStorage.setItem(storageKey, current);
        }
        setWidth(current || `${defaultWidth}px`);
      };

      document.body.style.cursor = 'col-resize';
      document.addEventListener('mousemove', onMove);
      document.addEventListener('mouseup', onUp);
      document.addEventListener('touchmove', onMove);
      document.addEventListener('touchend', onUp);
    },
    [width, min, max, defaultWidth, storageKey],
  );

  return { width, startDrag };
}
