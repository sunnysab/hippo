import { act, renderHook, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { emitRefresh } from '../utils/events';
import { useArticleSources } from './useArticleSources';

const apiGetMock = vi.fn();
vi.mock('../api', () => ({ apiGet: (path: string) => apiGetMock(path) }));

describe('useArticleSources', () => {
  beforeEach(() => {
    apiGetMock.mockReset();
    apiGetMock.mockResolvedValue({ sources: [{ source: 'feed', channels: 2 }] });
  });

  it('loads sources and refreshes subscription counts', async () => {
    const { result } = renderHook(useArticleSources);
    await waitFor(() => expect(result.current.sources[0]?.channels).toBe(2));
    apiGetMock.mockResolvedValue({ sources: [{ source: 'feed', channels: 3 }] });
    act(() => { emitRefresh(); });
    await waitFor(() => expect(result.current.sources[0]?.channels).toBe(3));
    expect(apiGetMock).toHaveBeenCalledTimes(2);
  });

  it('does not let a slow source response replace the newest answer', async () => {
    let release!: (value: unknown) => void;
    apiGetMock.mockImplementationOnce(() => new Promise((resolve) => { release = resolve; }));
    const { result } = renderHook(useArticleSources);
    act(() => { emitRefresh(); });
    await waitFor(() => expect(result.current.sources[0]?.channels).toBe(2));
    await act(async () => release({ sources: [{ source: 'feed', channels: 1 }] }));
    expect(result.current.sources[0]?.channels).toBe(2);
  });

  it('keeps cached choices on failure and allows retrying', async () => {
    const { result } = renderHook(useArticleSources);
    await waitFor(() => expect(result.current.sources[0]?.source).toBe('feed'));
    apiGetMock.mockRejectedValueOnce(new Error('offline'));
    await act(async () => result.current.reload());
    expect(result.current.error).toBe(true);
    expect(result.current.sources[0]?.source).toBe('feed');
    await act(async () => result.current.reload());
    expect(result.current.error).toBe(false);
  });

  it('stops refreshing sources after leaving the page', async () => {
    const { unmount } = renderHook(useArticleSources);
    await waitFor(() => expect(apiGetMock).toHaveBeenCalledTimes(1));
    unmount();
    act(() => { emitRefresh(); });
    expect(apiGetMock).toHaveBeenCalledTimes(1);
  });
});
