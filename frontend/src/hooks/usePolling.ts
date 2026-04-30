import { useRef, useEffect } from 'react';

export function usePolling(
  callback: () => Promise<void>,
  intervalMs: number | null,
): void {
  const savedCallback = useRef(callback);
  savedCallback.current = callback;

  useEffect(() => {
    if (intervalMs === null || intervalMs <= 0) return;
    const id = setInterval(() => {
      void savedCallback.current();
    }, intervalMs);
    return () => clearInterval(id);
  }, [intervalMs]);
}
