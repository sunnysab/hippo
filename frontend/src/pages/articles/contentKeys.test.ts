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
      'paragraph::Body',
      'image:7:Cover',
    ]);
  });

  it('adds a suffix when duplicate blocks share the same signature', () => {
    expect(buildContentBlockKeys([
      { type: 'paragraph', text: 'Repeat' },
      { type: 'paragraph', text: 'Repeat' },
    ])).toEqual([
      'paragraph::Repeat',
      'paragraph::Repeat#2',
    ]);
  });

  it('tells two transcript paragraphs with the same words apart', () => {
    // 转写稿里同一句话可能出现在不同时间点，锚点必须进签名。
    expect(buildContentBlockKeys([
      { type: 'paragraph', text: '对', t: 12 },
      { type: 'paragraph', text: '对', t: 340 },
    ])).toEqual([
      'paragraph:12:对',
      'paragraph:340:对',
    ]);
  });

  it('keys media blocks by what they play', () => {
    expect(buildContentBlockKeys([
      { type: 'video', provider: 'bilibili', video_id: 'BV1x' },
      { type: 'audio', url: '/api/media/7/audio' },
    ])).toEqual([
      'video:bilibili:BV1x',
      'audio:/api/media/7/audio',
    ]);
  });
});
