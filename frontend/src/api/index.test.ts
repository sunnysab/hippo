import { beforeEach, describe, expect, it, vi } from 'vitest';
import { apiGet, apiSend } from './index';

const jsonResponse = (body: Record<string, unknown>, init: ResponseInit = {}) =>
  new Response(JSON.stringify(body), {
    status: init.status ?? 200,
    headers: { 'Content-Type': 'application/json' },
  });

describe('api auth handling', () => {
  beforeEach(() => {
    window.location.hash = '#/groups';
  });

  it('throws an auth-tagged error for apiGet 401 responses', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse(
      { error: 'Session expired' },
      { status: 401 },
    )));

    await expect(apiGet('/api/group')).rejects.toMatchObject({
      code: 'AUTH_REQUIRED',
      message: 'Session expired',
    });
    expect(window.location.hash).toBe('#/settings/login');
  });

  it('throws an auth-tagged error for apiSend 401 responses', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse(
      { error: 'Please login again' },
      { status: 401 },
    )));

    await expect(apiSend('/api/login/start', 'POST', {})).rejects.toMatchObject({
      code: 'AUTH_REQUIRED',
      message: 'Please login again',
    });
    expect(window.location.hash).toBe('#/settings/login');
  });
});
