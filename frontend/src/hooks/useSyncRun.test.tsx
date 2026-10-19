import { act, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError } from '../api';
import { I18nProvider } from '../i18n';
import zhCN from '../i18n/zh-CN.json';
import { useSyncRun, wait, type SyncRunScope } from './useSyncRun';

const apiGetMock = vi.fn();
const apiSendMock = vi.fn();
const showToastMock = vi.fn();
const emitRefreshMock = vi.fn();

vi.mock('../api', async () => {
  const actual = await vi.importActual<typeof import('../api')>('../api');
  return {
    ...actual,
    apiGet: (...args: unknown[]) => apiGetMock(...args),
    apiSend: (...args: unknown[]) => apiSendMock(...args),
  };
});

vi.mock('./useToast', async () => {
  const actual = await vi.importActual<typeof import('./useToast')>('./useToast');
  return { ...actual, useToast: () => ({ showToast: showToastMock }) };
});

vi.mock('../utils/events', async () => {
  const actual = await vi.importActual<typeof import('../utils/events')>('../utils/events');
  return { ...actual, emitRefresh: () => emitRefreshMock() };
});

function Harness({ scope }: { scope: SyncRunScope }) {
  const { runSync, task, running } = useSyncRun();
  return (
    <div>
      <button type="button" onClick={() => { void runSync(scope); }}>run</button>
      <span data-testid="status">{task?.status || '-'}</span>
      <span data-testid="running">{running ? 'yes' : 'no'}</span>
    </div>
  );
}

const renderHarness = (scope: SyncRunScope = {}) => render(
  <I18nProvider>
    <Harness scope={scope} />
  </I18nProvider>,
);

const clickRun = async () => {
  await act(async () => {
    fireEvent.click(screen.getByRole('button', { name: 'run' }));
  });
};

const tick = async (ms: number) => {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
};

describe('useSyncRun', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('runs the page scope and follows the queued job to its result', async () => {
    apiSendMock.mockResolvedValue({ task_id: 'task-1', status: 'queued' });
    apiGetMock
      .mockResolvedValueOnce({ task_id: 'task-1', status: 'running' })
      .mockResolvedValueOnce({ task_id: 'task-1', status: 'success' });

    renderHarness({ source: 'bilibili' });
    await clickRun();

    expect(apiSendMock).toHaveBeenCalledWith(
      '/api/settings/run',
      'POST',
      { source: 'bilibili' },
      expect.anything(),
    );
    expect(showToastMock).toHaveBeenCalledWith(zhCN['sync.runQueued']);
    expect(screen.getByTestId('running').textContent).toBe('yes');

    await tick(2000);
    expect(apiGetMock).toHaveBeenCalledWith('/api/settings/tasks/task-1', expect.anything());
    expect(screen.getByTestId('status').textContent).toBe('running');

    await tick(2000);
    expect(screen.getByTestId('status').textContent).toBe('success');
    expect(showToastMock).toHaveBeenLastCalledWith(zhCN['sync.status.success']);
    expect(emitRefreshMock).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId('running').textContent).toBe('no');

    // A finished job is not worth asking about again.
    await tick(6000);
    expect(apiGetMock).toHaveBeenCalledTimes(2);
  });

  it('sends one group as the scope', async () => {
    apiSendMock.mockResolvedValue({});

    renderHarness({ groupId: '7' });
    await clickRun();

    expect(apiSendMock).toHaveBeenCalledWith(
      '/api/settings/run',
      'POST',
      { group_id: 7 },
      expect.anything(),
    );
  });

  it('sends an explicit list as the scope', async () => {
    apiSendMock.mockResolvedValue({});

    renderHarness({ bizList: ['biz-a', 'biz-b'] });
    await clickRun();

    expect(apiSendMock).toHaveBeenCalledWith(
      '/api/settings/run',
      'POST',
      { biz_list: ['biz-a', 'biz-b'] },
      expect.anything(),
    );
  });

  it('keeps every scope the page provides instead of narrowing to one', async () => {
    apiSendMock.mockResolvedValue({});

    renderHarness({ source: 'bilibili', groupId: '7', bizList: ['biz-a'] });
    await clickRun();

    expect(apiSendMock).toHaveBeenCalledWith(
      '/api/settings/run',
      'POST',
      { source: 'bilibili', group_id: 7, biz_list: ['biz-a'] },
      expect.anything(),
    );
  });

  it('does not widen an explicit empty list into every subscription', async () => {
    apiSendMock.mockResolvedValue({});

    renderHarness({ bizList: [] });
    await clickRun();

    expect(apiSendMock).toHaveBeenCalledWith(
      '/api/settings/run',
      'POST',
      { biz_list: [] },
      expect.anything(),
    );
  });

  it('asks for every subscription when the page has no narrower scope', async () => {
    apiSendMock.mockResolvedValue({ task_id: 'task-1', status: 'success' });

    renderHarness({ source: '', groupId: '' });
    await clickRun();

    expect(apiSendMock).toHaveBeenCalledWith('/api/settings/run', 'POST', {}, expect.anything());
    // The job was already over when the answer arrived: nothing left to poll.
    expect(screen.getByTestId('status').textContent).toBe('success');
    expect(apiGetMock).not.toHaveBeenCalled();
  });

  it('sends console runs to the admin endpoint, with their source', async () => {
    apiSendMock.mockResolvedValue({ task_id: 'task-1', status: 'success' });

    renderHarness({ siteWide: true, source: 'weixin' });
    await clickRun();

    expect(apiSendMock).toHaveBeenCalledWith(
      '/api/settings/admin/run',
      'POST',
      { source: 'weixin' },
      expect.anything(),
    );
  });

  it('never narrows a console run with a reader scope', async () => {
    apiSendMock.mockResolvedValue({ task_id: 'task-1', status: 'success' });

    renderHarness({ siteWide: true, groupId: '3', bizList: ['biz-a'] });
    await clickRun();

    expect(apiSendMock).toHaveBeenCalledWith(
      '/api/settings/admin/run',
      'POST',
      {},
      expect.anything(),
    );
  });

  it('reports a refused scope instead of claiming it was queued', async () => {
    apiSendMock.mockRejectedValue(
      new ApiError('Task conflict', { code: 'sync.task_conflict', status: 409 }),
    );

    renderHarness({ source: 'bilibili' });
    await clickRun();

    expect(showToastMock).toHaveBeenCalledWith(zhCN['sync.taskConflict']);
    expect(showToastMock).not.toHaveBeenCalledWith(zhCN['sync.runQueued']);
    expect(apiGetMock).not.toHaveBeenCalled();
    expect(screen.getByTestId('running').textContent).toBe('no');
  });

  it('treats any 409 as a refused scope', async () => {
    apiSendMock.mockRejectedValue(new ApiError('Conflict', { status: 409 }));

    renderHarness({ source: 'bilibili' });
    await clickRun();

    expect(showToastMock).toHaveBeenCalledWith(zhCN['sync.taskConflict']);
  });

  it('stops polling when the page goes away', async () => {
    apiSendMock.mockResolvedValue({ task_id: 'task-1', status: 'running' });
    apiGetMock.mockResolvedValue({ task_id: 'task-1', status: 'running' });

    const view = renderHarness({ source: 'bilibili' });
    await clickRun();
    await tick(2000);
    expect(apiGetMock).toHaveBeenCalledTimes(1);

    view.unmount();

    await tick(10000);
    expect(apiGetMock).toHaveBeenCalledTimes(1);
    expect(emitRefreshMock).not.toHaveBeenCalled();
  });

  it('reclaims the abort listener on every completed poll', async () => {
    const addSpy = vi.spyOn(AbortSignal.prototype, 'addEventListener');
    const removeSpy = vi.spyOn(AbortSignal.prototype, 'removeEventListener');
    apiSendMock.mockResolvedValue({ task_id: 'task-1', status: 'running' });
    apiGetMock.mockResolvedValue({ task_id: 'task-1', status: 'running' });

    renderHarness({ source: 'bilibili' });
    await clickRun();
    const signal = (apiSendMock.mock.calls[0][3] as { signal: AbortSignal }).signal;
    for (let i = 0; i < 5; i += 1) await tick(2000);

    const onOurSignal = (spy: typeof addSpy) => spy.mock.calls.filter(
      (call, index) => call[0] === 'abort' && spy.mock.contexts[index] === signal,
    ).length;
    const added = onOurSignal(addSpy);
    const removed = onOurSignal(removeSpy);
    // Only the wait that is currently in flight may still hold a listener.
    expect(added).toBe(6);
    expect(added - removed).toBe(1);

    addSpy.mockRestore();
    removeSpy.mockRestore();
  });

  it('resolves a wait on an already-aborted signal without arming a timer', async () => {
    const controller = new AbortController();
    controller.abort();
    const timerSpy = vi.spyOn(window, 'setTimeout');

    await expect(wait(2000, controller.signal)).resolves.toBeUndefined();
    expect(timerSpy).not.toHaveBeenCalled();

    timerSpy.mockRestore();
  });

  it('drops a slow answer from a run that has been superseded', async () => {
    let releaseOld: (payload: unknown) => void = () => {};
    apiSendMock
      .mockResolvedValueOnce({ task_id: 'old', status: 'running' })
      .mockResolvedValueOnce({ task_id: 'new', status: 'success' });
    apiGetMock.mockImplementation((path: string) => {
      if (path === '/api/settings/tasks/old') {
        return new Promise((resolve) => { releaseOld = resolve; });
      }
      return Promise.resolve({ task_id: 'new', status: 'success' });
    });

    renderHarness({ source: 'bilibili' });
    await clickRun();
    await tick(2000);

    await clickRun();
    await tick(2000);
    expect(screen.getByTestId('status').textContent).toBe('success');
    expect(showToastMock).toHaveBeenLastCalledWith(zhCN['sync.status.success']);

    await act(async () => {
      releaseOld({ task_id: 'old', status: 'failed', error: 'stale failure' });
      await Promise.resolve();
    });

    expect(screen.getByTestId('status').textContent).toBe('success');
    expect(showToastMock).not.toHaveBeenCalledWith('stale failure');
    expect(screen.getByTestId('running').textContent).toBe('no');
  });
});
