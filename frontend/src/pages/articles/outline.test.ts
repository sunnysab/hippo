import { describe, expect, it } from 'vitest';
import { buildOutline, headingAnchorId } from './outline';
import type { ArticleContentBlock } from '../../store/articles';

describe('buildOutline', () => {
  it('keeps heading order and clamps levels to what the reader renders', () => {
    const blocks: ArticleContentBlock[] = [
      { type: 'paragraph', text: '正文' },
      { type: 'heading', level: 1, text: '一级' },
      { type: 'heading', level: 3, text: '三级' },
      { type: 'heading', level: 6, text: '六级' },
    ];

    expect(buildOutline(blocks)).toEqual([
      { id: headingAnchorId(1), level: 2, text: '一级' },
      { id: headingAnchorId(2), level: 3, text: '三级' },
      { id: headingAnchorId(3), level: 4, text: '六级' },
    ]);
  });

  it('skips empty headings instead of linking to nothing', () => {
    expect(buildOutline([{ type: 'heading', level: 2, text: '   ' }])).toEqual([]);
  });

  it('treats missing content as no outline', () => {
    expect(buildOutline(null)).toEqual([]);
  });
});

describe('source-neutral outline anchors', () => {
  it('numbers only nonempty renderable text blocks', async () => {
    const { isOutlineBlock } = await import('./outline');
    const blocks: ArticleContentBlock[] = [
      { type: 'video', url: '/api/media/1/video' }, { type: 'paragraph', text: ' ' },
      { type: 'paragraph', text: '\ufeff' },
      { type: 'paragraph', text: '\u0085' },
      { type: 'paragraph', text: 'Start' }, { type: 'image', image_id: 8 },
      { type: 'heading', text: 'Middle' }, { type: 'code', text: 'code()' },
      { type: 'link', text: 'URL' }, { type: 'text', text: 'Legacy' },
    ];
    expect(blocks.filter(isOutlineBlock).map((block) => block.text)).toEqual(['\u0085', 'Start', 'Middle', 'code()', 'URL', 'Legacy']);
  });

  it('compares content versions by instant, not timezone spelling', async () => {
    const { sameContentVersion } = await import('./outline');
    expect(sameContentVersion('2026-10-19T08:00:00+08:00', '2026-10-19T00:00:00Z')).toBe(true);
    expect(sameContentVersion(null, null)).toBe(true);
    expect(sameContentVersion(null, '2026-10-19T00:00:00Z')).toBe(false);
    expect(sameContentVersion('bad', 'other')).toBe(false);
  });
});
