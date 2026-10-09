import { useCallback, useEffect, useState, type RefObject } from 'react';
import { useI18n } from '../../i18n';
import type { OutlineItem } from './outline';

interface TableOfContentsProps {
  items: OutlineItem[];
  /** The scrolling reading pane the headings live in. */
  containerRef: RefObject<HTMLElement | null>;
}

/** A heading counts as current once it is within this band from the top. */
const ACTIVE_BAND = '0px 0px -72% 0px';

export function TableOfContents({ items, containerRef }: TableOfContentsProps) {
  const { t } = useI18n();
  const [activeId, setActiveId] = useState('');

  const jumpTo = useCallback((id: string) => {
    const container = containerRef.current;
    const target = container?.querySelector(`#${CSS.escape(id)}`);
    if (!container || !target) return;
    // Scroll the reading pane itself: `scrollIntoView` would also move the page
    // behind it, which reads as a jump.
    container.scrollTo({ top: (target as HTMLElement).offsetTop - 12, behavior: 'smooth' });
  }, [containerRef]);

  useEffect(() => {
    const container = containerRef.current;
    if (!container || items.length === 0) {
      setActiveId('');
      return;
    }

    const elements = items
      .map((item) => container.querySelector(`#${CSS.escape(item.id)}`))
      .filter((element): element is Element => element !== null);
    if (elements.length === 0) return;

    const observer = new IntersectionObserver(
      (entries) => {
        const visible = entries
          .filter((entry) => entry.isIntersecting)
          .sort((left, right) => left.boundingClientRect.top - right.boundingClientRect.top);
        if (visible[0]?.target.id) setActiveId(visible[0].target.id);
      },
      { root: container, rootMargin: ACTIVE_BAND, threshold: 0 },
    );
    elements.forEach((element) => observer.observe(element));
    return () => observer.disconnect();
  }, [containerRef, items]);

  return (
    <aside className="reader-toc" aria-label={t('articles.toc', '目录')}>
      <h2 className="reader-toc-title">{t('articles.toc', '目录')}</h2>
      <nav className="reader-toc-list">
        {items.map((item) => (
          <button
            key={item.id}
            className={`reader-toc-item is-level-${item.level}${item.id === activeId ? ' is-active' : ''}`}
            type="button"
            title={item.text}
            aria-current={item.id === activeId ? 'true' : undefined}
            onClick={() => jumpTo(item.id)}
          >
            {item.text}
          </button>
        ))}
      </nav>
    </aside>
  );
}
