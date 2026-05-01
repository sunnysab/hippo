import { describe, expect, it } from 'vitest';
import { buildContentBlockKeys } from './contentKeys';

describe('buildContentBlockKeys', () => {
  it('uses block content instead of array position for stable keys', () => {
    expect(buildContentBlockKeys([
      { type: 'heading', level: 2, text: 'Title' },
      { type: 'paragraph', text: 'Body' },
      { type: 'image', image_id: 7, alt: 'Cover' },
    ])).toEqual([
      'heading:2:Title',
      'paragraph:Body',
      'image:7:Cover',
    ]);
  });

  it('adds a suffix when duplicate blocks share the same signature', () => {
    expect(buildContentBlockKeys([
      { type: 'paragraph', text: 'Repeat' },
      { type: 'paragraph', text: 'Repeat' },
    ])).toEqual([
      'paragraph:Repeat',
      'paragraph:Repeat#2',
    ]);
  });
});
