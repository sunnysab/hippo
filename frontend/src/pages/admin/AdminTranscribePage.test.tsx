import { render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { AdminTranscribePage } from './AdminTranscribePage';

const apiGetMock = vi.fn();
const apiSendMock = vi.fn();
const showToastMock = vi.fn();

vi.mock('../../api', () => ({
  apiGet: (...args: unknown[]) => apiGetMock(...args),
  apiSend: (...args: unknown[]) => apiSendMock(...args),
}));

vi.mock('../../i18n', () => ({
  useI18n: () => ({
    t: (_key: string, fallback?: string) => fallback || _key,
  }),
}));

vi.mock('../../hooks/useToast', () => ({
  useToast: () => ({ showToast: showToastMock }),
}));

describe('AdminTranscribePage media archiving copy', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    apiGetMock.mockResolvedValue({
      max_duration_seconds: 14400,
      keep_media_bitrate: '128k',
      keep_media: true,
    });
  });

  it('describes media duration, video copy and the download limits without promising an unlimited archive', async () => {
    render(<AdminTranscribePage />);

    expect(await screen.findByText('Max media duration (seconds)')).toBeTruthy();
    expect(screen.getByText('Archived audio bitrate')).toBeTruthy();
    expect(screen.getByText('Archive audio and video for playback')).toBeTruthy();

    const hint = screen.getByText(/video is copied as-is up to 720p/);
    expect(hint.textContent).toContain('2 GiB');
    expect(hint.textContent).toContain('not backfilled automatically');
  });

  it('keeps the instance-wide parameters off a single source page', async () => {
    render(<AdminTranscribePage />);

    // 凭证与代理属于某个来源，不该出现在共享的转写设置里。
    expect(await screen.findByText('转写服务')).toBeTruthy();
    expect(screen.queryByText('B站 Cookie')).toBeNull();
    expect(screen.queryByText('YouTube cookies（Netscape cookies.txt 内容）')).toBeNull();
  });
});
