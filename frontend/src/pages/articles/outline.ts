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
