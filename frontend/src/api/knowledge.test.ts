import { afterEach, describe, expect, it, vi } from 'vitest';
import { ApiError } from './index';
import { onToast } from '../utils/events';
import {
  createDocument,
  createKnowledgeBase,
  deleteDocument,
  deleteKnowledgeBase,
  getDocument,
  getDocumentContent,
  getKnowledgeBase,
  listDocuments,
  listKnowledgeBases,
  renameKnowledgeBase,
  retryDocument,
  searchKnowledgeBase,
  uploadDocumentFile,
} from './knowledge';

const jsonResponse = (body: Record<string, unknown>, status = 200) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });

const stubFetch = (respond: (init: RequestInit) => Response) => {
  const fetchMock = vi.fn(async (_path: string, init: RequestInit = {}) => respond(init));
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
};

const documentBody = (overrides: Record<string, unknown> = {}) => ({
  id: 'doc-1',
  knowledge_base_id: 'kb-1',
  filename: 'guide.pdf',
  mime_type: 'application/pdf',
  size_bytes: 12,
  state: 'ready',
  attempts: 1,
  error_code: null,
  error_message: null,
  page_count: 3,
  version: 2,
  created_at: '2024-01-01T00:00:00Z',
  updated_at: '2024-01-01T00:00:00Z',
  ...overrides,
});

describe('knowledge JSON client', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    window.location.hash = '';
  });

  it('lists knowledge bases and unwraps the collection', async () => {
    const fetchMock = stubFetch(() => jsonResponse({
      knowledge_bases: [{ id: 'kb-1', name: 'Notes', created_at: 'a', updated_at: 'b' }],
      limit: 20,
      offset: 0,
    }));

    await expect(listKnowledgeBases()).resolves.toEqual([
      { id: 'kb-1', name: 'Notes', created_at: 'a', updated_at: 'b' },
    ]);
    expect(fetchMock.mock.calls[0][0]).toBe('/api/knowledge-base');
  });

  it('encodes ids, queries and pagination', async () => {
    const fetchMock = stubFetch(() => jsonResponse({
      knowledge_bases: [],
      knowledge_base: { id: 'kb/1 a', name: 'Renamed', created_at: 'a', updated_at: 'b' },
      hits: [],
      chunks: [],
      document: documentBody(),
      next_offset: null,
    }));

    await listKnowledgeBases({ limit: 5, offset: 10 });
    await getKnowledgeBase('kb/1 a');
    await renameKnowledgeBase('kb/1 a', 'Renamed');
    await getDocumentContent('doc/1', { limit: 100, offset: 0, page_no: 2 });
    await searchKnowledgeBase('kb/1', 'a&b', { document_id: 'doc/1', limit: 3 });

    expect(fetchMock.mock.calls[0][0]).toBe('/api/knowledge-base?limit=5&offset=10');
    expect(fetchMock.mock.calls[1][0]).toBe('/api/knowledge-base/kb%2F1%20a');
    expect(fetchMock.mock.calls[2][0]).toBe('/api/knowledge-base/kb%2F1%20a');
    expect(fetchMock.mock.calls[2][1]).toMatchObject({ method: 'PATCH' });
    expect(fetchMock.mock.calls[3][0]).toBe('/api/document/doc%2F1/content?limit=100&offset=0&page_no=2');
    expect(fetchMock.mock.calls[4][0]).toBe(
      '/api/knowledge-base/kb%2F1/search?q=a%26b&limit=3&document_id=doc%2F1',
    );
  });

  it('omits undefined query parameters', async () => {
    const fetchMock = stubFetch(() => jsonResponse({ hits: [], next_offset: null }));

    await searchKnowledgeBase('kb-1', 'term');
    expect(fetchMock.mock.calls[0][0]).toBe('/api/knowledge-base/kb-1/search?q=term');
  });

  it('sends JSON bodies for create and document registration', async () => {
    const fetchMock = stubFetch(() => jsonResponse({
      knowledge_base: { id: 'kb-1', name: 'Notes', created_at: 'a', updated_at: 'b' },
      document: documentBody(),
    }));

    await createKnowledgeBase('Notes');
    await createDocument('kb-1', { filename: 'guide.pdf', mime_type: 'application/pdf' });

    expect(fetchMock.mock.calls[0][0]).toBe('/api/knowledge-base');
    expect(fetchMock.mock.calls[0][1]).toMatchObject({
      method: 'POST',
      body: JSON.stringify({ name: 'Notes' }),
    });
    expect(fetchMock.mock.calls[1][0]).toBe('/api/knowledge-base/kb-1/document');
    expect(fetchMock.mock.calls[1][1]).toMatchObject({
      body: JSON.stringify({ filename: 'guide.pdf', mime_type: 'application/pdf' }),
    });
  });

  it('treats a 204 delete as resolved void', async () => {
    stubFetch(() => new Response(null, { status: 204 }));

    await expect(deleteKnowledgeBase('kb-1')).resolves.toBeUndefined();
    await expect(deleteDocument('doc-1')).resolves.toBeUndefined();
  });

  it('keeps the prepared retry state instead of assuming a re-upload', async () => {
    stubFetch(() => jsonResponse({ document: documentBody({ state: 'pending_upload' }) }));

    await expect(retryDocument('doc-1')).resolves.toMatchObject({ state: 'pending_upload' });
  });

  it('rejects when a 2xx response is missing the resource', async () => {
    stubFetch(() => jsonResponse({ ok: true }));

    await expect(getDocument('doc-1')).rejects.toMatchObject({
      name: 'ApiError',
      message: 'Response is missing document.',
    });
    await expect(listDocuments('kb-1')).rejects.toMatchObject({
      message: 'Response is missing documents.',
    });
  });

  it('keeps the server code for a 404', async () => {
    stubFetch(() => jsonResponse({ error: 'Document not found.', code: 'document.not_found' }, 404));

    await expect(getDocument('doc-1')).rejects.toMatchObject({
      code: 'document.not_found',
      status: 404,
    });
  });

  it('routes 401 through the shared auth handler', async () => {
    window.location.hash = '#/knowledge';
    stubFetch(() => jsonResponse({ error: 'nope' }, 401));

    await expect(listDocuments('kb-1')).rejects.toMatchObject({ code: 'AUTH_REQUIRED' });
    expect(window.location.hash).toBe('#/login');
  });

  it('forwards the caller signal', async () => {
    const controller = new AbortController();
    const fetchMock = stubFetch(() => jsonResponse({ document: documentBody() }));

    await getDocument('doc-1', { signal: controller.signal });
    expect(fetchMock.mock.calls[0][1]).toMatchObject({ signal: controller.signal });
  });
});

class FakeXhr {
  static instances: FakeXhr[] = [];

  static openError: Error | null = null;

  method = '';

  url = '';

  headers: Record<string, string> = {};

  sentBody: unknown = null;

  aborted = false;

  status = 0;

  responseText = '';

  upload: { onprogress: ((event: ProgressEvent) => void) | null } = { onprogress: null };

  onload: (() => void) | null = null;

  onerror: (() => void) | null = null;

  onabort: (() => void) | null = null;

  constructor() {
    FakeXhr.instances.push(this);
  }

  open(method: string, url: string) {
    if (FakeXhr.openError) throw FakeXhr.openError;
    this.method = method;
    this.url = url;
  }

  setRequestHeader(key: string, value: string) {
    this.headers[key] = value;
  }

  send(body: unknown) {
    this.sentBody = body;
  }

  abort() {
    this.aborted = true;
    this.onabort?.();
  }

  emitProgress(loaded: number, total: number, lengthComputable = true) {
    this.upload.onprogress?.({ loaded, total, lengthComputable } as unknown as ProgressEvent);
  }
}

const lastXhr = () => FakeXhr.instances[FakeXhr.instances.length - 1];

const respond = (xhr: FakeXhr, status: number, body: Record<string, unknown>) => {
  xhr.status = status;
  xhr.responseText = JSON.stringify(body);
  xhr.onload?.();
};

const file = new File(['%PDF-1.4'], 'guide.pdf', { type: 'application/pdf' });

const installXhr = () => {
  FakeXhr.instances = [];
  FakeXhr.openError = null;
  vi.stubGlobal('XMLHttpRequest', FakeXhr);
};

describe('uploadDocumentFile', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.useRealTimers();
    window.location.hash = '';
  });

  it('PUTs the raw File with the canonical MIME type', async () => {
    installXhr();
    const pending = uploadDocumentFile('doc/1', file, 'application/pdf');
    const xhr = lastXhr();

    expect(xhr.method).toBe('PUT');
    expect(xhr.url).toBe('/api/document/doc%2F1/file');
    expect(xhr.headers).toEqual({
      'Content-Type': 'application/pdf',
      Accept: 'application/json',
    });
    expect(xhr.sentBody).toBe(file);

    respond(xhr, 202, { document: documentBody({ state: 'processing' }) });
    await expect(pending).resolves.toMatchObject({ id: 'doc-1', state: 'processing' });
  });

  it('reports client byte progress only when lengthComputable', async () => {
    installXhr();
    const onProgress = vi.fn();
    const pending = uploadDocumentFile('doc-1', file, 'application/pdf', { onProgress });
    const xhr = lastXhr();

    xhr.emitProgress(4, 8);
    xhr.emitProgress(4, 0, false);
    expect(onProgress).toHaveBeenCalledTimes(1);
    expect(onProgress).toHaveBeenCalledWith({ loaded: 4, total: 8 });

    respond(xhr, 202, { document: documentBody() });
    await pending;
  });

  it('resolves a 202 response that carries a valid document envelope', async () => {
    installXhr();
    const pending = uploadDocumentFile('doc-1', file, 'text/plain');
    respond(lastXhr(), 202, { document: documentBody({ state: 'queued' }) });

    await expect(pending).resolves.toMatchObject({ state: 'queued' });
  });

  it('rejects a 2xx response without a document', async () => {
    installXhr();
    const pending = uploadDocumentFile('doc-1', file, 'application/pdf');
    respond(lastXhr(), 202, { ok: true });

    await expect(pending).rejects.toThrow('Response is missing document.');
  });

  it('rejects with the server code for 429 and 503', async () => {
    installXhr();
    const busy = uploadDocumentFile('doc-1', file, 'application/pdf');
    respond(lastXhr(), 429, {
      error: 'Another upload is already in progress.',
      code: 'document.upload_busy',
    });
    await expect(busy).rejects.toMatchObject({ code: 'document.upload_busy', status: 429 });

    const failed = uploadDocumentFile('doc-1', file, 'application/pdf');
    respond(lastXhr(), 503, { error: 'Upload failed.', code: 'document.persistence_failed' });
    await expect(failed).rejects.toMatchObject({ code: 'document.persistence_failed', status: 503 });
  });

  it('routes 401 through the shared auth handler', async () => {
    installXhr();
    window.location.hash = '#/knowledge';
    const pending = uploadDocumentFile('doc-1', file, 'application/pdf');
    respond(lastXhr(), 401, { error: 'Session expired' });

    await expect(pending).rejects.toMatchObject({ code: 'AUTH_REQUIRED', status: 401 });
    expect(window.location.hash).toBe('#/login');
  });

  it('rejects a malformed body without inventing an error code', async () => {
    installXhr();
    const pending = uploadDocumentFile('doc-1', file, 'application/pdf');
    const xhr = lastXhr();
    xhr.status = 400;
    xhr.responseText = '<html>bad</html>';
    xhr.onload?.();

    const error = await pending.catch((err: unknown) => err);
    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({ status: 400 });
    expect((error as ApiError).code).toBeUndefined();
  });

  it('reports network failures with a fixed code', async () => {
    installXhr();
    const pending = uploadDocumentFile('doc-1', file, 'application/pdf');
    lastXhr().onerror?.();

    await expect(pending).rejects.toMatchObject({ code: 'NETWORK' });
  });

  it('reports the client timeout after 300000ms', async () => {
    installXhr();
    vi.useFakeTimers();
    const pending = uploadDocumentFile('doc-1', file, 'application/pdf');
    const xhr = lastXhr();
    const error = pending.catch((err: unknown) => err);

    await vi.advanceTimersByTimeAsync(299999);
    expect(xhr.aborted).toBe(false);
    await vi.advanceTimersByTimeAsync(1);

    expect(xhr.aborted).toBe(true);
    expect(await error).toMatchObject({ code: 'TIMEOUT' });
  });

  it('aborts an in-flight request when the caller signal fires', async () => {
    installXhr();
    const controller = new AbortController();
    const pending = uploadDocumentFile('doc-1', file, 'application/pdf', {
      signal: controller.signal,
    });
    const xhr = lastXhr();
    const error = pending.catch((err: unknown) => err);

    controller.abort();

    expect(xhr.aborted).toBe(true);
    expect(await error).toMatchObject({ name: 'AbortError' });
  });

  it('does not send when the signal is already aborted', async () => {
    installXhr();
    const controller = new AbortController();
    controller.abort();

    const error = await uploadDocumentFile('doc-1', file, 'application/pdf', {
      signal: controller.signal,
    }).catch((err: unknown) => err);

    expect(error).toMatchObject({ name: 'AbortError' });
    expect(FakeXhr.instances).toHaveLength(0);
  });

  it('clears callbacks and signal listeners after settling', async () => {
    installXhr();
    const controller = new AbortController();
    const onProgress = vi.fn();
    const pending = uploadDocumentFile('doc-1', file, 'application/pdf', {
      signal: controller.signal,
      onProgress,
    });
    const xhr = lastXhr();

    respond(xhr, 202, { document: documentBody() });
    await pending;

    expect(xhr.onload).toBeNull();
    expect(xhr.upload.onprogress).toBeNull();

    // The abort listener is gone, so a late abort cannot abort or settle again.
    controller.abort();
    xhr.emitProgress(1, 2);
    expect(onProgress).not.toHaveBeenCalled();
    expect(xhr.aborted).toBe(false);
  });

  it('ignores a captured late onload after the upload already settled', async () => {
    installXhr();
    window.location.hash = '#/knowledge';
    const onProgress = vi.fn();
    const toasts: string[] = [];
    const unsubscribe = onToast((message) => toasts.push(message));
    const pending = uploadDocumentFile('doc-1', file, 'application/pdf', { onProgress });
    const xhr = lastXhr();
    const lateOnload = xhr.onload;

    respond(xhr, 202, { document: documentBody() });
    await expect(pending).resolves.toMatchObject({ id: 'doc-1' });

    // A handler captured before settling must not toast, redirect, report
    // progress or settle a second time when it fires late with a 401.
    xhr.status = 401;
    xhr.responseText = JSON.stringify({ error: 'Session expired' });
    lateOnload?.();
    xhr.emitProgress(1, 2);

    expect(toasts).toEqual([]);
    expect(window.location.hash).toBe('#/knowledge');
    expect(onProgress).not.toHaveBeenCalled();
    unsubscribe();
  });

  it('clears state when open throws synchronously', async () => {
    installXhr();
    FakeXhr.openError = new Error('open failed');
    const onProgress = vi.fn();

    await expect(
      uploadDocumentFile('doc-1', file, 'application/pdf', { onProgress }),
    ).rejects.toThrow('open failed');

    const xhr = lastXhr();
    expect(xhr.onload).toBeNull();
    expect(xhr.upload.onprogress).toBeNull();
  });
});
