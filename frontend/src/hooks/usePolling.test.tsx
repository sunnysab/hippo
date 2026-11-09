import { act, renderHook } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { usePolling } from './usePolling';

describe('serial polling', () => {
  afterEach(() => vi.useRealTimers());
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
});
