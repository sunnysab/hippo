import type { ArticleContentBlock, ArticlePayload } from '../../store/articles';

const headingPrefix = (level: number | undefined): string =>
  '#'.repeat(Math.min(Math.max(level ?? 2, 2), 4));

const blockToMarkdown = (block: ArticleContentBlock): string => {
  if (block.type === 'heading') return `${headingPrefix(block.level)} ${block.text ?? ''}`.trim();
  if (block.type === 'paragraph') return (block.text ?? '').trim();
  if (block.type === 'image') {
    return block.image_id ? `![${block.alt ?? ''}](/api/image/${block.image_id})` : '';
  }
  // Code blocks arrive with their fences still in the text; the reader strips
  // them for display, and an export must keep them.
  if (block.type === 'code') return (block.text ?? '').trim();
  return '';
};

/** Serialize the stored content blocks back to Markdown for export. */
export const articleToMarkdown = (payload: ArticlePayload): string => {
  const blocks = Array.isArray(payload.content) ? payload.content : [];
  const body = blocks.map(blockToMarkdown).filter((part) => part.length > 0);
  return [`# ${(payload.article.title || '').trim()}`, ...body].join('\n\n').trimEnd();
};
