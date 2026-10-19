import type { ArticleContentBlock } from '../../store/articles';

export interface OutlineItem {
  /** Must match the id the reader renders on the heading element. */
  id: string;
  level: number;
  text: string;
}

/** The heading id the reader uses for the block at `index`. */
export const headingAnchorId = (index: number): string => `article-heading-${index}`;

/**
 * Headings of the open article, in reading order.
 *
 * WeChat articles often have no real headings at all (they style paragraphs
 * instead), so an empty result is normal and the caller hides the outline.
 */
export const buildOutline = (blocks: ArticleContentBlock[] | null | undefined): OutlineItem[] => {
  if (!Array.isArray(blocks)) return [];
  const items: OutlineItem[] = [];
  blocks.forEach((block, index) => {
    if (block.type !== 'heading') return;
    const text = (block.text ?? '').trim();
    if (!text) return;
    items.push({
      id: headingAnchorId(index),
      level: Math.min(Math.max(Number(block.level) || 2, 2), 4),
      text,
    });
  });
  return items;
};

/** Matches the backend snapshot numbering, independent of media/image blocks. */
export const isOutlineBlock = (block: ArticleContentBlock): boolean => (
  ['paragraph', 'heading', 'code', 'link', 'text'].includes(block.type)
  && typeof block.text === 'string'
  && Boolean(block.text.trim())
);

export interface GeneratedTopic {
  title: string;
  start_block: number;
}

export interface GeneratedSection extends GeneratedTopic {
  children: GeneratedTopic[];
}

export interface ArticleOutline {
  state: 'missing' | 'stale' | 'queued' | 'processing' | 'ready' | 'failed';
  content_updated_at: string | null;
  sections: GeneratedSection[];
  can_generate: boolean;
  model: string | null;
  updated_at: string | null;
  error_code: string | null;
}

export const outlineBlockSelector = (index: number): string => `[data-outline-block="${index}"]`;

export const sameContentVersion = (left: string | null, right: string | null): boolean => (
  left === right || (left !== null && right !== null && Date.parse(left) === Date.parse(right))
);

/** Scroll only the reader on desktop; use the page when mobile has no scroll pane. */
export function jumpToOutline(container: HTMLElement, target: HTMLElement) {
  const reduced = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
  const behavior = reduced ? 'instant' : 'smooth';
  const scrolls = /auto|scroll/.test(getComputedStyle(container).overflowY);
  if (scrolls) {
    container.scrollTo({ top: container.scrollTop + target.getBoundingClientRect().top - container.getBoundingClientRect().top - 24, behavior });
  } else {
    window.scrollTo({ top: window.scrollY + target.getBoundingClientRect().top - 88, behavior });
  }
  if (!reduced) target.animate?.([{ backgroundColor: 'var(--info-soft)' }, { backgroundColor: 'transparent' }], { duration: 1000 });
}
