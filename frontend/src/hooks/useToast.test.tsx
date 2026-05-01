import { render, screen } from '@testing-library/react';
import { useEffect } from 'react';
import { describe, expect, it, vi } from 'vitest';
import { ToastProvider, useToast } from './useToast';

function TriggerToast() {
  const { showToast } = useToast();

  useEffect(() => {
    showToast('Saved');
  }, [showToast]);

  return null;
}

describe('ToastProvider', () => {
  it('clears pending timers on unmount', () => {
    vi.useFakeTimers();
    const clearTimeoutSpy = vi.spyOn(window, 'clearTimeout');

    const view = render(
      <ToastProvider>
        <TriggerToast />
      </ToastProvider>,
    );

    expect(screen.getByText('Saved')).toBeTruthy();

    view.unmount();

    expect(clearTimeoutSpy).toHaveBeenCalled();
    vi.useRealTimers();
  });
});
