import { render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { QueuePanel } from './QueuePanel';

const settingsStateMock = vi.fn();

vi.mock('../../store/settings', () => ({
  useSettingsState: () => settingsStateMock(),
}));

vi.mock('../../i18n', () => ({
  useI18n: () => ({
    t: (_key: string, fallback?: string) => fallback || _key,
  }),
}));

vi.mock('../../utils/format', () => ({
  escapeHtml: (value: unknown) => String(value ?? ''),
  formatRelativeTime: () => 'recently',
}));

describe('QueuePanel', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('renders queue counters, heartbeat and failed items', () => {
    settingsStateMock.mockReturnValue({
      state: {
        syncStatus: {
          worker_heartbeat_at: '2026-05-02T11:59:00.000Z',
          queue: {
            articles: { pending: 3, processing: 1, failed: 2, done: 10 },
            images: { pending: 7, failed: 1 },
            failed_items: [
              {
                biz: 'MzA==',
                nickname: 'Demo',
                sn: 'sn-1',
                attempts: 3,
                retryable: false,
                last_error: 'timeout',
                updated_at: null,
              },
            ],
          },
        },
      },
    });

    render(<QueuePanel />);

    expect(screen.getByText('Article bodies pending')).toBeTruthy();
    expect(screen.getByText('3')).toBeTruthy();
    expect(screen.getByText('worker last seen recently')).toBeTruthy();
    expect(screen.getByText('Demo')).toBeTruthy();
    expect(screen.getByText('3 tries')).toBeTruthy();
    expect(screen.getByText('timeout')).toBeTruthy();
  });

  it('falls back to zeros when the worker has not published stats yet', () => {
    settingsStateMock.mockReturnValue({ state: { syncStatus: null } });

    render(<QueuePanel />);

    expect(screen.getByText('no worker heartbeat yet')).toBeTruthy();
    expect(screen.getAllByText('0')).toHaveLength(5);
  });
});
