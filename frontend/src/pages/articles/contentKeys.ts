import type { ArticleContentBlock } from '../../store/articles';

const buildContentBlockSignature = (block: ArticleContentBlock): string => {
  switch (block.type) {
    case 'heading':
      return `heading:${String(block.level || '')}:${block.text || ''}`;
    case 'image':
      return `image:${String(block.image_id || '')}:${block.alt || ''}`;
    case 'code':
      return `code:${block.text || ''}`;
    case 'video':
      return `video:${block.provider || ''}:${block.video_id || block.embed_url || ''}`;
    case 'audio':
      return `audio:${block.url || ''}`;
    case 'paragraph':
    default:
      return `paragraph:${String(block.t ?? '')}:${block.text || ''}`;
  }
};

export const buildContentBlockKeys = (blocks: ArticleContentBlock[]): string[] => {
  const seen = new Map<string, number>();

  return blocks.map((block) => {
    const signature = buildContentBlockSignature(block);
    const nextCount = (seen.get(signature) || 0) + 1;
    seen.set(signature, nextCount);
    return nextCount === 1 ? signature : `${signature}#${nextCount}`;
  });
};
