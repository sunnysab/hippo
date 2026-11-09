import { useEffect } from 'react';

/** Serial polling with request cancellation and no rescheduling after unmount. */
export function usePolling(load: (signal: AbortSignal) => Promise<void>, delay: number | null, immediate = true) {
  useEffect(() => {
    if (delay === null) return;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      if (controller.signal.aborted) return;
      try {
        await load(controller.signal);
      } catch {
        // The owner renders request errors; a transient failure still gets retried.
      } finally {
        if (!controller.signal.aborted) timer = setTimeout(() => void poll(), delay);
      }
    };
    if (immediate) queueMicrotask(() => void poll());
    else timer = setTimeout(() => void poll(), delay);
    return () => {
      controller.abort();
      clearTimeout(timer);
    };
  }, [load, delay, immediate]);
}
