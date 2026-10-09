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
