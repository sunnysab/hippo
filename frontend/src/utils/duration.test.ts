import { describe, expect, it } from 'vitest';
import { formatDuration, timestampHref } from './duration';

describe('timestampHref', () => {
  it('adds the offset as the first query parameter', () => {
    expect(timestampHref('https://www.bilibili.com/video/BV1x', 92.7)).toBe(
      'https://www.bilibili.com/video/BV1x?t=92',
    );
  });

  it('appends to an existing query string', () => {
    expect(timestampHref('https://www.youtube.com/watch?v=abc', 12)).toBe(
      'https://www.youtube.com/watch?v=abc&t=12',
    );
  });

  it('has nothing to offer without a link or at the start', () => {
    expect(timestampHref('', 30)).toBeNull();
    expect(timestampHref('https://x.test/v', 0)).toBeNull();
  });

  it('keeps formatDuration as the anchor label', () => {
    expect(formatDuration(3725)).toBe('1:02:05');
  });
});
