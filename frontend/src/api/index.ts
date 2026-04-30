const handleAuthError = (message?: string) => {
  const t = (window as unknown as Record<string, unknown>).__hippo_t as ((k: string, f: string) => string) | undefined;
  const fallback = t ? t('login.sessionExpired', 'Session expired. Please login again.') : 'Session expired. Please login again.';
  window.dispatchEvent(new CustomEvent('hippo:toast', { detail: message || fallback }));
  window.location.hash = '#/settings';
};

export const apiGet = async (path: string): Promise<Record<string, unknown>> => {
  const res = await fetch(path, { headers: { 'Accept': 'application/json' } });
  if (!res.ok) {
    const payload = await res.json().catch(() => ({})) as Record<string, string>;
    if (res.status === 401) {
      handleAuthError(payload.error);
    }
    throw new Error(payload.error || `Request failed: ${res.status}`);
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
      const timeoutErr = new Error('Request timeout');
      (timeoutErr as unknown as Record<string, unknown>).code = 'TIMEOUT';
      throw timeoutErr;
    }
    throw err;
  } finally {
    if (timeoutId) clearTimeout(timeoutId);
  }
  if (!res.ok && res.status !== 204) {
    const payload = await res.json().catch(() => ({})) as Record<string, string>;
    if (res.status === 401) {
      handleAuthError(payload.error);
    }
    throw new Error(payload.error || `Request failed: ${res.status}`);
  }
  if (res.status === 204) return {};
  return res.json() as Promise<Record<string, unknown>>;
};
