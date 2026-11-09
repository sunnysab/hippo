import { useCallback, useEffect, useRef } from 'react';

/** Serial, visibility-aware polling. Manual refreshes join the same request loop. */
export function usePolling(load: (signal: AbortSignal) => Promise<void>, delay: number | null, immediate = true) {
  const refreshRef = useRef<() => void>(() => {});
  const refresh = useCallback(() => refreshRef.current(), []);
  useEffect(() => {
    if (delay === null) return;
    let disposed = false;
    let running = false;
    let pending = false;
    let controller: AbortController | undefined;
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      if (disposed || document.hidden) return;
      if (running) { pending = true; return; }
      clearTimeout(timer);
      pending = false;
      running = true;
      controller = new AbortController();
      try {
        await load(controller.signal);
      } catch {
        // The owner renders errors. The next scheduled poll retries failures.
      } finally {
        running = false;
        if (!disposed && !document.hidden) {
          if (pending) queueMicrotask(() => void poll());
          else timer = setTimeout(() => void poll(), delay);
        }
      }
    };
    const request = () => { clearTimeout(timer); void poll(); };
    const visibility = () => {
      clearTimeout(timer);
      if (document.hidden) controller?.abort();
      else request();
    };
    refreshRef.current = request;
    document.addEventListener('visibilitychange', visibility);
    if (immediate) queueMicrotask(request);
    else timer = setTimeout(request, delay);
    return () => {
      disposed = true;
      controller?.abort();
      clearTimeout(timer);
      refreshRef.current = () => {};
      document.removeEventListener('visibilitychange', visibility);
    };
  }, [load, delay, immediate]);
  return refresh;
}
