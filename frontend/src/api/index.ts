import { emitToast } from '../utils/events';

export class ApiError extends Error {
  code?: string;
  status?: number;

  constructor(message: string, options: { code?: string; status?: number } = {}) {
    super(message);
    this.name = 'ApiError';
    this.code = options.code;
    this.status = options.status;
  }
}

const createApiError = (message: string, options: { code?: string; status?: number } = {}) => {
  return new ApiError(message, options);
};

export const isAuthError = (error: unknown): error is ApiError => {
  return error instanceof ApiError && error.code === 'AUTH_REQUIRED';
};

const handleAuthError = (message?: string) => {
  const t = (window as unknown as Record<string, unknown>).__hippo_t as ((k: string, f: string) => string) | undefined;
  const fallback = t ? t('login.sessionExpired', 'Session expired. Please login again.') : 'Session expired. Please login again.';
  emitToast(message || fallback);
  window.location.hash = '#/settings/login';
};

export const apiGet = async (path: string): Promise<Record<string, unknown>> => {
  const res = await fetch(path, { headers: { 'Accept': 'application/json' } });
  if (!res.ok) {
    const payload = await res.json().catch(() => ({})) as Record<string, string>;
    if (res.status === 401) {
      handleAuthError(payload.error);
      throw createApiError(payload.error || 'Authentication required', {
        code: 'AUTH_REQUIRED',
        status: res.status,
      });
    }
    throw createApiError(payload.error || `Request failed: ${res.status}`, { status: res.status });
  }
  return res.json() as Promise<Record<string, unknown>>;
};

export const apiSend = async (
  path: string,
  method: string,
  body: Record<string, unknown>,
  options: { timeoutMs?: number } = {},
): Promise<Record<string, unknown>> => {
  const timeoutMs = Number(options.timeoutMs);
  const withTimeout = Number.isFinite(timeoutMs) && timeoutMs > 0;
  const controller = withTimeout ? new AbortController() : null;
  const timeoutId = withTimeout
    ? setTimeout(() => { controller!.abort(); }, Math.trunc(timeoutMs))
    : null;
  let res: Response;
  try {
    res = await fetch(path, {
      method,
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {}),
      signal: controller?.signal,
    });
  } catch (err) {
    if (withTimeout && (err as Error)?.name === 'AbortError') {
      throw createApiError('Request timeout', { code: 'TIMEOUT' });
    }
    throw err;
  } finally {
    if (timeoutId) clearTimeout(timeoutId);
  }
  if (!res.ok && res.status !== 204) {
    const payload = await res.json().catch(() => ({})) as Record<string, string>;
    if (res.status === 401) {
      handleAuthError(payload.error);
      throw createApiError(payload.error || 'Authentication required', {
        code: 'AUTH_REQUIRED',
        status: res.status,
      });
    }
    throw createApiError(payload.error || `Request failed: ${res.status}`, { status: res.status });
  }
  if (res.status === 204) return {};
  return res.json() as Promise<Record<string, unknown>>;
};
