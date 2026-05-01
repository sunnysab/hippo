import { describe, expect, it, vi } from 'vitest';
import { formatRelativeTime } from './format';

describe('formatRelativeTime', () => {
  it('uses translation keys instead of hardcoded text', () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-05-01T12:00:00.000Z'));

    const t = (key: string, fallback?: string) => {
      if (key === 'time.minutesAgo') return '{n}m';
      return fallback || key;
    };

    expect(formatRelativeTime('2026-05-01T11:55:00.000Z', t)).toBe('5m');

    vi.useRealTimers();
  });
});
