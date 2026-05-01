import { render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { ErrorBoundary } from './ErrorBoundary';

function Crash() {
  throw new Error('boom');
  return null;
}

describe('ErrorBoundary', () => {
  it('renders fallback content when a child throws during render', () => {
    const consoleErrorSpy = vi.spyOn(console, 'error').mockImplementation(() => {});

    render(
      <ErrorBoundary fallback={<div>Fallback</div>}>
        <Crash />
      </ErrorBoundary>,
    );

    expect(screen.getByText('Fallback')).toBeTruthy();
    consoleErrorSpy.mockRestore();
  });
});
