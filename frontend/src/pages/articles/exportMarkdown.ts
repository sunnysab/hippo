import type { ArticleContentBlock, ArticlePayload } from '../../store/articles';

const headingPrefix = (level: number | undefined): string =>
  '#'.repeat(Math.min(Math.max(level ?? 2, 2), 4));

/** `[mm:ss]`, the anchor a transcript paragraph carries (hours only when needed). */
export const anchorLabel = (seconds: number | undefined): string => {
  if (seconds === undefined || seconds === null || !Number.isFinite(seconds)) return '';
  const total = Math.max(Math.floor(seconds), 0);
  const hours = Math.floor(total / 3600);
  const minutes = String(Math.floor((total % 3600) / 60)).padStart(2, '0');
  const rest = String(total % 60).padStart(2, '0');
  return hours > 0 ? `${hours}:${minutes}:${rest}` : `${minutes}:${rest}`;
};

const blockToMarkdown = (block: ArticleContentBlock): string => {
  if (block.type === 'heading') return `${headingPrefix(block.level)} ${block.text ?? ''}`.trim();
  if (block.type === 'paragraph') {
    const text = (block.text ?? '').trim();
    const label = anchorLabel(block.t);
    return label && text ? `**[${label}]** ${text}` : text;
  }
  if (block.type === 'video') return block.embed_url ? `[视频](${block.embed_url})` : '';
  if (block.type === 'audio') return block.url ? `[音频](${block.url})` : '';
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
