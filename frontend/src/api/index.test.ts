import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError, apiGet, apiSend, handleAuthError, isAuthError } from './index';

const jsonResponse = (body: Record<string, unknown>, status = 200) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });

/** Stubs fetch and returns the mock so a test can assert on path/init. */
const stubFetch = (respond: (init: RequestInit) => Response | Promise<Response>) => {
  const fetchMock = vi.fn((_path: string, init: RequestInit = {}) => Promise.resolve(respond(init)));
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
};

/** A fetch that only settles when its (merged) signal aborts. */
const stubHangingFetch = () => stubFetch((init) => new Promise<Response>((_resolve, reject) => {
  init.signal?.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')));
}));

describe('api auth handling', () => {
  beforeEach(() => {
    window.location.hash = '#/groups';
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  it('throws an auth-tagged error for apiGet 401 responses', async () => {
    stubFetch(() => jsonResponse({ error: 'Session expired' }, 401));

    await expect(apiGet('/api/group')).rejects.toMatchObject({
      code: 'AUTH_REQUIRED',
      message: 'Session expired',
    });
    expect(window.location.hash).toBe('#/login');
  });

  it('throws an auth-tagged error for apiSend 401 responses', async () => {
    stubFetch(() => jsonResponse({ error: 'Please login again' }, 401));

    await expect(apiSend('/api/login/refresh', 'POST', {})).rejects.toMatchObject({
      code: 'AUTH_REQUIRED',
      message: 'Please login again',
    });
    expect(window.location.hash).toBe('#/login');
  });

  it('keeps the server code on non-401 failures', async () => {
    stubFetch(() => jsonResponse({
      error: 'The document is not ready.',
      code: 'document.not_ready',
    }, 409));

    const error = await apiGet('/api/document/x/content').catch((err: unknown) => err);
    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({ code: 'document.not_ready', status: 409 });
    expect(window.location.hash).toBe('#/groups');
  });

  it('does not redirect when silent is set', async () => {
    stubFetch(() => jsonResponse({ error: 'nope' }, 401));

    await expect(apiGet('/api/auth/me', { silent: true })).rejects.toMatchObject({
      code: 'AUTH_REQUIRED',
    });
    expect(window.location.hash).toBe('#/groups');
  });

  it('forwards a caller signal to fetch', async () => {
    const controller = new AbortController();
    const fetchMock = stubFetch(() => jsonResponse({ ok: true }));

    await apiGet('/api/group', { signal: controller.signal });
    expect(fetchMock.mock.calls[0][1]).toMatchObject({ signal: controller.signal });
  });

  it('surfaces the caller abort as an AbortError even with a timeout configured', async () => {
    stubHangingFetch();
    const controller = new AbortController();

    const pending = apiSend('/api/group', 'POST', {}, { timeoutMs: 60000, signal: controller.signal })
      .catch((err: unknown) => err);
    controller.abort();

    const error = await pending;
    expect(error).not.toBeInstanceOf(ApiError);
    expect(error).toMatchObject({ name: 'AbortError' });
  });

  it('reports an internal timeout as ApiError TIMEOUT', async () => {
    vi.useFakeTimers();
    stubHangingFetch();

    const pending = apiSend('/api/group', 'POST', {}, { timeoutMs: 50 }).catch((err: unknown) => err);
    await vi.advanceTimersByTimeAsync(60);

    const error = await pending;
    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({ code: 'TIMEOUT' });
  });

  it('keeps a transport AbortError when the internal timeout never fired', async () => {
    stubFetch(() => Promise.reject(new DOMException('aborted', 'AbortError')));
    const controller = new AbortController();

    const error = await apiSend('/api/group', 'POST', {}, {
      timeoutMs: 60000,
      signal: controller.signal,
    }).catch((err: unknown) => err);

    expect(error).not.toBeInstanceOf(ApiError);
    expect(error).toMatchObject({ name: 'AbortError' });
  });

  it('resolves 204 responses without parsing a body', async () => {
    stubFetch(() => new Response(null, { status: 204 }));

    await expect(apiSend('/api/document/x', 'DELETE', {})).resolves.toEqual({});
  });

  it('exports the shared 401 handler', () => {
    expect(handleAuthError).toBeTypeOf('function');
    expect(isAuthError(new ApiError('x', { code: 'AUTH_REQUIRED' }))).toBe(true);
  });
});
