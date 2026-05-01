import { describe, expect, it, vi } from 'vitest';
import { emitRefresh, emitToast, onRefresh, onToast } from './events';

describe('events helpers', () => {
  it('subscribes and unsubscribes refresh listeners', () => {
    const listener = vi.fn();
    const unsubscribe = onRefresh(listener);

    emitRefresh();
    expect(listener).toHaveBeenCalledTimes(1);

    unsubscribe();
    emitRefresh();
    expect(listener).toHaveBeenCalledTimes(1);
  });

  it('delivers typed toast payloads', () => {
    const listener = vi.fn();
    const unsubscribe = onToast(listener);

    emitToast('Saved');

    expect(listener).toHaveBeenCalledWith('Saved');
    unsubscribe();
  });
});
