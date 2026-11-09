import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { I18nProvider } from '../../i18n';
import { ReportPage } from './ReportPage';

const mocks = vi.hoisted(() => ({
  get: vi.fn(),
  send: vi.fn(),
  toast: vi.fn(),
  user: { id: 1, timezone: 'Asia/Shanghai' as string | undefined },
}));

vi.mock('../../api', () => ({ apiGet: mocks.get, apiSend: mocks.send }));
vi.mock('../../hooks/useAuth', () => ({ useAuth: () => ({ user: mocks.user }) }));
vi.mock('../../hooks/useToast', () => ({ useToast: () => ({ showToast: mocks.toast }) }));

function report(title = 'Original article') {
  return {
    date: '2026-11-09',
    timezone: 'Asia/Shanghai',
    total: 1,
    groups: [{
      id: 1,
      name: 'Tech',
      articles: [{
        id: 42,
        title,
        digest: null,
        link: 'https://example.test',
        nickname: 'Example',
        biz: 'example',
      }],
    }],
  };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: Error) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

function renderPage() {
  render(<I18nProvider><ReportPage /></I18nProvider>);
}

function changeDate(date: string) {
  fireEvent.change(screen.getByLabelText('日期'), { target: { value: date } });
}

beforeEach(() => {
  vi.clearAllMocks();
  mocks.user.timezone = 'Asia/Shanghai';
  mocks.get.mockImplementation(async (path: string) => {
    if (path === '/api/group') return { groups: [{ id: 1, name: 'Tech' }] };
    if (path === '/api/report/setting') {
      return { enabled: false, send_hour: 8, recipients: [], group_ids: [], include_read: false };
    }
    return report();
  });
  mocks.send.mockResolvedValue({});
});

describe('ReportPage', () => {
  it('hides the old report while the new date loads, and retries a failed request', async () => {
    const next = deferred<ReturnType<typeof report>>();
    renderPage();
    await screen.findByText('Original article');
    mocks.get.mockImplementationOnce(() => next.promise);

    changeDate('2026-11-10');

    expect(screen.queryByText('Original article')).toBeNull();
    expect(screen.queryByText(/共 1 篇/)).toBeNull();
    expect((screen.getByRole('button', { name: '立即发送' }) as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByText('加载中…')).toBeTruthy();

    await act(async () => next.reject(new Error('Report request failed')));

    expect(screen.getByRole('alert').textContent).toContain('Report request failed');
    expect(screen.queryByText('这一天没有新文章')).toBeNull();
    expect(screen.queryByText('Original article')).toBeNull();
    expect((screen.getByRole('button', { name: '立即发送' }) as HTMLButtonElement).disabled).toBe(true);

    mocks.get.mockResolvedValueOnce(report('Retried article'));
    fireEvent.click(screen.getByRole('button', { name: '重试' }));

    await screen.findByText('Retried article');
    expect(screen.queryByRole('alert')).toBeNull();
    expect(mocks.get).toHaveBeenCalledWith('/api/report/2026-11-10');
    expect(mocks.get.mock.calls.filter(([path]) => path === '/api/report/setting')).toHaveLength(1);
  });

  it('hides the old report when filters change and shows a failed filter request', async () => {
    const filtered = deferred<ReturnType<typeof report>>();
    renderPage();
    await screen.findByText('Original article');
    mocks.get.mockImplementationOnce(() => filtered.promise);

    fireEvent.click(screen.getByRole('checkbox', { name: 'Tech' }));

    expect(screen.queryByText('Original article')).toBeNull();
    expect(screen.queryByText(/共 1 篇/)).toBeNull();
    expect(screen.getByText('加载中…')).toBeTruthy();
    expect((screen.getByRole('button', { name: '立即发送' }) as HTMLButtonElement).disabled).toBe(true);

    await act(async () => filtered.reject(new Error('Filtered report failed')));

    expect(screen.getByRole('alert').textContent).toContain('Filtered report failed');
    expect(screen.queryByText('Original article')).toBeNull();
    expect(screen.queryByText('这一天没有新文章')).toBeNull();
    expect(mocks.get.mock.calls.some(([path]) => String(path).endsWith('?group_ids=1'))).toBe(true);
  });

  it('ignores a slower response for a previous date', async () => {
    const slow = deferred<ReturnType<typeof report>>();
    const latest = deferred<ReturnType<typeof report>>();
    renderPage();
    await screen.findByText('Original article');

    mocks.get.mockImplementationOnce(() => slow.promise);
    changeDate('2026-11-10');
    mocks.get.mockImplementationOnce(() => latest.promise);
    changeDate('2026-11-11');

    await act(async () => latest.resolve(report('Latest article')));
    await screen.findByText('Latest article');
    await act(async () => slow.resolve(report('Stale article')));

    expect(screen.getByText('Latest article')).toBeTruthy();
    expect(screen.queryByText('Stale article')).toBeNull();
  });

  it('reloads when returning to a date whose earlier result is still stored', async () => {
    const previous = deferred<ReturnType<typeof report>>();
    const revisited = deferred<ReturnType<typeof report>>();
    renderPage();
    await screen.findByText('Original article');
    const originalDate = (screen.getByLabelText('日期') as HTMLInputElement).value;
    mocks.get.mockImplementationOnce(() => previous.promise);
    changeDate('2026-11-10');
    mocks.get.mockImplementationOnce(() => revisited.promise);
    changeDate(originalDate);
    expect(screen.queryByText('Original article')).toBeNull();
    expect(screen.getByText('加载中…')).toBeTruthy();
    await act(async () => {
      previous.resolve(report('Stale article'));
      revisited.resolve(report('Updated original date'));
    });
    expect(screen.getByText('Updated original date')).toBeTruthy();
    expect(screen.queryByText('Stale article')).toBeNull();
  });

  it.each([
    ['Asia/Shanghai', '2026-11-09'],
    ['America/New_York', '2026-11-08'],
    [undefined, '2026-11-09'],
  ])('uses the user timezone %s for the initial date', async (timezone, expected) => {
    vi.useFakeTimers({ toFake: ['Date'] });
    vi.setSystemTime(new Date('2026-11-08T16:30:00Z'));
    mocks.user.timezone = timezone;
    try {
      renderPage();
      expect((screen.getByLabelText('日期') as HTMLInputElement).value).toBe(expected);
      await screen.findByText('Original article');
    } finally {
      vi.useRealTimers();
    }
  });

  it('disables sending until the current send request finishes', async () => {
    const pending = deferred<object>();
    mocks.send.mockReturnValueOnce(pending.promise);
    renderPage();
    await screen.findByText('Original article');
    const button = screen.getByRole('button', { name: '立即发送' }) as HTMLButtonElement;

    fireEvent.click(button);
    fireEvent.click(button);

    expect(button.disabled).toBe(true);
    expect(mocks.send).toHaveBeenCalledTimes(1);

    await act(async () => pending.resolve({}));
    await waitFor(() => expect(button.disabled).toBe(false));
  });
});
