import { act, renderHook } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { usePolling } from './usePolling';

describe('serial polling', () => {
  afterEach(() => { vi.useRealTimers(); vi.restoreAllMocks(); });
  it('does not overlap requests or reschedule after unmount', async () => {
    vi.useFakeTimers();
    let resolve!: () => void;
    const load = vi.fn((signal: AbortSignal) => new Promise<void>((done) => { expect(signal.aborted).toBe(false); resolve = done; }));
    const { unmount } = renderHook(() => usePolling(load, 1000));
    await act(() => vi.advanceTimersByTimeAsync(10000));
    expect(load).toHaveBeenCalledTimes(1);
    unmount();
    expect(load.mock.calls[0][0]?.aborted).toBe(true);
    await act(async () => { resolve(); await vi.advanceTimersByTimeAsync(10000); });
    expect(load).toHaveBeenCalledTimes(1);
  });
  it('retries after failure and supports delayed initial polling', async () => {
    vi.useFakeTimers();
    const load = vi.fn().mockRejectedValueOnce(new Error('offline')).mockResolvedValue(undefined);
    const { unmount } = renderHook(() => usePolling(load, 1000, false));
    expect(load).not.toHaveBeenCalled();
    await act(() => vi.advanceTimersByTimeAsync(2000));
    expect(load).toHaveBeenCalledTimes(2);
    unmount();
  });
  it('pauses when hidden and refreshes immediately when visible', async () => {
    vi.useFakeTimers();
    const hidden = vi.spyOn(document, 'hidden', 'get').mockReturnValue(true);
    const load = vi.fn().mockResolvedValue(undefined);
    const { unmount } = renderHook(() => usePolling(load, 1000));
    await act(() => vi.advanceTimersByTimeAsync(5000));
    expect(load).not.toHaveBeenCalled();
    hidden.mockReturnValue(false);
    await act(async () => document.dispatchEvent(new Event('visibilitychange')));
    expect(load).toHaveBeenCalledTimes(1);
    hidden.mockReturnValue(true);
    await act(async () => document.dispatchEvent(new Event('visibilitychange')));
    await act(() => vi.advanceTimersByTimeAsync(5000));
    expect(load).toHaveBeenCalledTimes(1);
    unmount();
  });
  it('coalesces manual refreshes behind a pending request', async () => {
    vi.useFakeTimers();
    let resolve!: () => void;
    const load = vi.fn().mockImplementationOnce(() => new Promise<void>((done) => { resolve = done; }))
      .mockResolvedValue(undefined);
    const { result, unmount } = renderHook(() => usePolling(load, 1000));
    await act(() => vi.advanceTimersByTimeAsync(0));
    act(() => { result.current(); result.current(); });
    expect(load).toHaveBeenCalledTimes(1);
    await act(async () => { resolve(); await vi.advanceTimersByTimeAsync(0); });
    expect(load).toHaveBeenCalledTimes(2);
    unmount();
  });

});
