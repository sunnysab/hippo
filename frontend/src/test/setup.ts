import { afterEach, beforeEach, vi } from 'vitest';
import { cleanup } from '@testing-library/react';

beforeEach(() => {
  vi.restoreAllMocks();
});

afterEach(() => {
  cleanup();
});
