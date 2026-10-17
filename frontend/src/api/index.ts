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

export const handleAuthError = (message?: string) => {
  const t = (window as unknown as Record<string, unknown>).__hippo_t as ((k: string, f: string) => string) | undefined;
  const fallback = t ? t('login.sessionExpired', 'Session expired. Please login again.') : 'Session expired. Please login again.';
  emitToast(message || fallback);
  window.location.hash = '#/login';
};

const authError = (payload: { error?: string }, status: number, silent?: boolean) => {
  // Callers that expect an anonymous answer (the auth probe) opt out of the
  // global redirect so they can render the login form instead.
  if (!silent) handleAuthError(payload.error);
  return createApiError(payload.error || 'Authentication required', {
    code: 'AUTH_REQUIRED',
    status,
  });
};

export const apiGet = async (
  path: string,
  options: { silent?: boolean; signal?: AbortSignal } = {},
): Promise<Record<string, unknown>> => {
  const res = await fetch(path, {
    headers: { 'Accept': 'application/json' },
    signal: options.signal,
  });
  if (!res.ok) {
    const payload = await res.json().catch(() => ({})) as { error?: string; code?: string };
    if (res.status === 401) throw authError(payload, res.status, options.silent);
    throw createApiError(payload.error || `Request failed: ${res.status}`, {
      code: payload.code,
      status: res.status,
    });
  }
  return res.json() as Promise<Record<string, unknown>>;
};

export const apiSend = async (
  path: string,
  method: string,
  body: Record<string, unknown>,
  options: { timeoutMs?: number; silent?: boolean; signal?: AbortSignal } = {},
): Promise<Record<string, unknown>> => {
  const timeoutMs = Number(options.timeoutMs);
  const withTimeout = Number.isFinite(timeoutMs) && timeoutMs > 0;
  const controller = withTimeout ? new AbortController() : null;
  const timeoutId = withTimeout
    ? setTimeout(() => { controller!.abort(); }, Math.trunc(timeoutMs))
    : null;
  // The timer aborts our own controller; a caller's signal stays untouched and
  // its abort must surface as the original AbortError.
  const signal = options.signal
    ? (controller ? AbortSignal.any([options.signal, controller.signal]) : options.signal)
    : (controller?.signal);
  let res: Response;
  try {
    res = await fetch(path, {
      method,
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {}),
      signal,
    });
  } catch (err) {
    // Only our own timer can turn an AbortError into TIMEOUT. An abort that
    // arrives before the timer fires belongs to the caller or the transport and
    // must surface as the original AbortError.
    if (controller?.signal.aborted && (err as Error)?.name === 'AbortError') {
      throw createApiError('Request timeout', { code: 'TIMEOUT' });
    }
    throw err;
  } finally {
    if (timeoutId) clearTimeout(timeoutId);
  }
  if (!res.ok && res.status !== 204) {
    const payload = await res.json().catch(() => ({})) as { error?: string; code?: string };
    if (res.status === 401) throw authError(payload, res.status, options.silent);
    throw createApiError(payload.error || `Request failed: ${res.status}`, {
      code: payload.code,
      status: res.status,
    });
  }
  if (res.status === 204) return {};
  return res.json() as Promise<Record<string, unknown>>;
};
