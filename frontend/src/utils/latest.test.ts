import { describe, expect, it, vi } from 'vitest';
import { createLatestOnly } from './latest';

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((nextResolve, nextReject) => {
    resolve = nextResolve;
    reject = nextReject;
  });
  return { promise, resolve, reject };
}

describe('createLatestOnly', () => {
  it('applies only the latest completed request result', async () => {
    const first = deferred<string>();
    const second = deferred<string>();
    const apply = vi.fn();
    const latestOnly = createLatestOnly(apply);

    const firstRun = latestOnly(() => first.promise);
    const secondRun = latestOnly(() => second.promise);

    second.resolve('second');
    await secondRun;

    first.resolve('first');
    await firstRun;

    expect(apply).toHaveBeenCalledTimes(1);
    expect(apply).toHaveBeenCalledWith('second');
  });
});
