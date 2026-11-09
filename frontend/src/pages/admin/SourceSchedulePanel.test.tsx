import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { apiGet, apiSend } from '../../api';
import { I18nProvider } from '../../i18n';
import { SourceSchedulePanel } from './SourceSchedulePanel';

vi.mock('../../api', () => ({
  apiGet: vi.fn(),
  apiSend: vi.fn(),
  isAuthError: vi.fn(() => false),
}));
vi.mock('../../hooks/useToast', () => ({
  useToast: () => ({ showToast: vi.fn() }),
}));

const settings = { source_intervals_days: { feed: 1, youtube: 3, bilibili: 1, podcast: 1 } };

describe('SourceSchedulePanel', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(apiGet).mockResolvedValue(settings);
    vi.mocked(apiSend).mockResolvedValue(settings);
  });

  it('saves only the selected source without overwriting the others', async () => {
    render(<I18nProvider><SourceSchedulePanel source="youtube" /></I18nProvider>);
    const interval = await screen.findByRole('spinbutton', { name: '默认更新间隔（天）' });
    expect((interval as HTMLInputElement).value).toBe('3');
    fireEvent.change(interval, { target: { value: '7' } });
    fireEvent.click(screen.getByRole('button', { name: '保存设置' }));
    await waitFor(() => expect(apiSend).toHaveBeenCalledWith('/api/settings', 'PATCH', {
      source_intervals_days: { youtube: 7 },
    }));
  });

  it('keeps saving disabled after a failed read and allows retrying', async () => {
    vi.mocked(apiGet).mockRejectedValueOnce(new Error('source settings unavailable'));
    render(<I18nProvider><SourceSchedulePanel source="feed" /></I18nProvider>);
    expect((await screen.findByRole('alert')).textContent).toContain('source settings unavailable');
    expect((screen.getByRole('button', { name: '保存设置' }) as HTMLButtonElement).disabled).toBe(true);
    expect(screen.queryByRole('spinbutton')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: '重试' }));
    expect((await screen.findByRole('spinbutton', { name: '默认更新间隔（天）' }) as HTMLInputElement).value).toBe('1');
    expect((screen.getByRole('button', { name: '保存设置' }) as HTMLButtonElement).disabled).toBe(false);
    expect(apiSend).not.toHaveBeenCalled();
  });

  it('refuses fractional update intervals before writing', async () => {
    render(<I18nProvider><SourceSchedulePanel source="feed" /></I18nProvider>);
    const interval = await screen.findByRole('spinbutton', { name: '默认更新间隔（天）' });
    fireEvent.change(interval, { target: { value: '1.5' } });
    fireEvent.submit(interval.closest('form')!);
    expect(apiSend).not.toHaveBeenCalled();
  });
});
