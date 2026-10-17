import { StrictMode, type ReactNode } from 'react';
import { act, renderHook, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ApiError } from '../../api/index';
import type { KnowledgeDocument } from '../../api/knowledge';
import { useUploads } from './useUploads';

vi.mock('../../api/knowledge', () => ({
  createDocument: vi.fn(),
  getDocument: vi.fn(),
  retryDocument: vi.fn(),
  uploadDocumentFile: vi.fn(),
  deleteDocument: vi.fn(),
}));

import {
  createDocument,
  deleteDocument,
  getDocument,
  retryDocument,
  uploadDocumentFile,
} from '../../api/knowledge';

const mockCreate = vi.mocked(createDocument);
const mockGet = vi.mocked(getDocument);
const mockRetry = vi.mocked(retryDocument);
const mockUpload = vi.mocked(uploadDocumentFile);
const mockDelete = vi.mocked(deleteDocument);

interface Deferred<T> {
  promise: Promise<T>;
  resolve: (value: T) => void;
  reject: (reason?: unknown) => void;
}

const deferred = <T,>(): Deferred<T> => {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
};

const documentBody = (overrides: Partial<KnowledgeDocument> = {}): KnowledgeDocument => ({
  id: 'doc-1',
  knowledge_base_id: 'kb-1',
  filename: 'guide.pdf',
  mime_type: 'application/pdf',
  size_bytes: 8,
  state: 'pending_upload',
  attempts: 0,
  error_code: null,
  error_message: null,
  page_count: null,
  version: 1,
  created_at: '2024-01-01T00:00:00Z',
  updated_at: '2024-01-01T00:00:00Z',
  ...overrides,
});

const pdfFile = (name = 'guide.pdf', content = '%PDF-1.4') =>
  new File([content], name, { type: 'application/pdf' });

const oversizedPdf = () => {
  const file = new File(['%PDF-1.4'], 'huge.pdf', { type: 'application/pdf' });
  Object.defineProperty(file, 'size', { value: 64 * 1024 * 1024 + 1 });
  return file;
};

/**
 * The real client settles an aborted request as AbortError, so every stubbed
 * request rejects on abort instead of staying pending forever.
 */
const abortable = <T,>(pending: Deferred<T>, signal: AbortSignal | undefined) => {
  signal?.addEventListener('abort', () => {
    pending.reject(new DOMException('The request was aborted.', 'AbortError'));
  });
};

const stubCreate = (pending: Deferred<KnowledgeDocument>) => {
  const state: { signal?: AbortSignal } = {};
  mockCreate.mockImplementationOnce((_baseId, _input, options) => {
    state.signal = options?.signal;
    abortable(pending, options?.signal);
    return pending.promise;
  });
  return { signal: () => state.signal };
};

const stubUpload = (pending: Deferred<KnowledgeDocument>) => {
  const state: {
    signal?: AbortSignal;
    progress: ((progress: { loaded: number; total: number }) => void) | null;
  } = { progress: null };
  mockUpload.mockImplementationOnce((_documentId, _file, _mime, options) => {
    state.signal = options?.signal;
    state.progress = options?.onProgress ?? null;
    abortable(pending, options?.signal);
    return pending.promise;
  });
  return {
    signal: () => state.signal,
    emitProgress: (loaded: number, total: number) => state.progress?.({ loaded, total }),
  };
};

afterEach(() => {
  vi.resetAllMocks();
});

describe('useUploads validation', () => {
  it('fails unsupported, empty and oversized files without any request', () => {
    const { result } = renderHook(() => useUploads());

    act(() => {
      result.current.addFiles('kb-1', [
        new File(['x'], 'notes.exe'),
        new File([], 'empty.pdf'),
        oversizedPdf(),
      ]);
    });

    expect(mockCreate).not.toHaveBeenCalled();
    expect(mockUpload).not.toHaveBeenCalled();
    expect(result.current.tasks.map((task) => [task.phase, task.errorCode])).toEqual([
      ['failed', 'document.unsupported'],
      ['failed', 'document.empty'],
      ['failed', 'document.too_large'],
    ]);
  });

  it('accepts PDF/TXT by case-insensitive extension and ignores File.type', async () => {
    const first = deferred<KnowledgeDocument>();
    const second = deferred<KnowledgeDocument>();
    stubCreate(first);
    stubCreate(second);
    mockUpload
      .mockResolvedValueOnce(documentBody({ id: 'doc-a', filename: 'GUIDE.PDF', state: 'queued' }))
      .mockResolvedValueOnce(documentBody({ id: 'doc-b', filename: 'notes.TXT', state: 'queued' }));
    const { result } = renderHook(() => useUploads());
    await act(async () => {
      result.current.addFiles('kb-1', [
        new File(['%PDF-1.4'], 'GUIDE.PDF', { type: '' }),
        new File(['hello'], 'notes.TXT', { type: '' }),
      ]);
    });

    expect(mockCreate.mock.calls.map((call) => call[1])).toEqual([
      { filename: 'GUIDE.PDF', mime_type: 'application/pdf' },
      { filename: 'notes.TXT', mime_type: 'text/plain' },
    ]);
    expect(result.current.tasks.map((task) => task.mime_type)).toEqual(['application/pdf', 'text/plain']);

    await act(async () => {
      first.resolve(documentBody({ id: 'doc-a', filename: 'GUIDE.PDF', state: 'pending_upload' }));
      second.resolve(documentBody({ id: 'doc-b', filename: 'notes.TXT', state: 'pending_upload' }));
    });

    await waitFor(() => expect(mockUpload).toHaveBeenCalledTimes(2));
    expect(mockUpload.mock.calls.map((call) => call[2])).toEqual(['application/pdf', 'text/plain']);
  });
});

describe('useUploads scheduling', () => {
  it('runs at most two files and dispatches the third FIFO after a slot frees', async () => {
    const first = deferred<KnowledgeDocument>();
    const second = deferred<KnowledgeDocument>();
    const third = deferred<KnowledgeDocument>();
    stubCreate(first);
    stubCreate(second);
    stubCreate(third);

    const { result } = renderHook(() => useUploads());
    await act(async () => {
      result.current.addFiles('kb-1', [pdfFile('a.pdf'), pdfFile('b.pdf'), pdfFile('c.pdf')]);
    });

    expect(mockCreate.mock.calls.map((call) => call[1].filename)).toEqual(['a.pdf', 'b.pdf']);
    expect(result.current.tasks.map((task) => task.phase)).toEqual(['registering', 'registering', 'waiting']);

    await act(async () => {
      first.resolve(documentBody({ id: 'doc-a', filename: 'a.pdf', state: 'queued' }));
    });

    await waitFor(() => expect(mockCreate).toHaveBeenCalledTimes(3));
    expect(mockCreate.mock.calls[2][1].filename).toBe('c.pdf');
    expect(mockUpload).not.toHaveBeenCalled();
  });

  it('keeps the base frozen per task when another base is added later', async () => {
    const first = deferred<KnowledgeDocument>();
    const second = deferred<KnowledgeDocument>();
    stubCreate(first);
    stubCreate(second);

    const { result } = renderHook(() => useUploads());
    await act(async () => {
      result.current.addFiles('kb-1', [pdfFile('a.pdf')]);
    });
    await act(async () => {
      result.current.addFiles('kb-2', [pdfFile('b.pdf')]);
    });

    expect(mockCreate.mock.calls.map((call) => call[0])).toEqual(['kb-1', 'kb-2']);
    expect(result.current.tasks.map((task) => task.baseId)).toEqual(['kb-1', 'kb-2']);

    await act(async () => {
      first.resolve(documentBody({ id: 'doc-a', filename: 'a.pdf', state: 'queued' }));
      second.resolve(documentBody({ id: 'doc-b', filename: 'b.pdf', state: 'queued' }));
    });
  });

  it('fails one file without redoing a settled one', async () => {
    mockCreate
      .mockRejectedValueOnce(new ApiError('Bad request', { code: 'document.invalid', status: 400 }))
      .mockResolvedValueOnce(documentBody({ id: 'doc-2', filename: 'b.pdf', state: 'queued' }));

    const { result } = renderHook(() => useUploads());
    await act(async () => {
      result.current.addFiles('kb-1', [pdfFile('a.pdf'), pdfFile('b.pdf')]);
    });

    await waitFor(() => expect(result.current.tasks[1].phase).toBe('uploaded'));
    expect(result.current.tasks[0]).toMatchObject({ phase: 'failed', errorCode: 'document.invalid' });
    expect(result.current.paused).toBe(false);
    expect(mockCreate).toHaveBeenCalledTimes(2);
  });
});

describe('useUploads acknowledgement', () => {
  it('stays uploading at 100 percent until the server returns a document', async () => {
    mockCreate.mockResolvedValueOnce(documentBody({ id: 'doc-1', state: 'pending_upload' }));
    const pending = deferred<KnowledgeDocument>();
    const upload = stubUpload(pending);

    const { result } = renderHook(() => useUploads());
    await act(async () => {
      result.current.addFiles('kb-1', [pdfFile()]);
    });
    await waitFor(() => expect(result.current.tasks[0].phase).toBe('uploading'));

    act(() => {
      upload.emitProgress(8, 8);
    });

    expect(result.current.tasks[0]).toMatchObject({
      phase: 'uploading',
      loaded: 8,
      total: 8,
      document: { id: 'doc-1', state: 'pending_upload' },
    });

    await act(async () => {
      pending.resolve(documentBody({ id: 'doc-1', state: 'queued' }));
    });

    await waitFor(() => expect(result.current.tasks[0]).toMatchObject({
      phase: 'uploaded',
      document: { id: 'doc-1', state: 'queued' },
    }));
  });

  it('does not PUT when the created document omits its id', async () => {
    mockCreate.mockResolvedValueOnce({ ...documentBody(), id: undefined } as unknown as KnowledgeDocument);

    const { result } = renderHook(() => useUploads());
    await act(async () => {
      result.current.addFiles('kb-1', [pdfFile()]);
    });

    await waitFor(() => expect(result.current.tasks[0]).toMatchObject({
      phase: 'uncertain',
      errorCode: 'document.invalid_response',
    }));
    expect(mockUpload).not.toHaveBeenCalled();
  });

  it('reports uncertain instead of uploaded when the ack is still pending_upload', async () => {
    mockCreate.mockResolvedValueOnce(documentBody({ id: 'doc-1', state: 'pending_upload' }));
    mockUpload.mockResolvedValueOnce(documentBody({ id: 'doc-1', state: 'pending_upload' }));

    const { result } = renderHook(() => useUploads());
    await act(async () => {
      result.current.addFiles('kb-1', [pdfFile()]);
    });

    await waitFor(() => expect(result.current.tasks[0]).toMatchObject({
      phase: 'uncertain',
      document: { id: 'doc-1', state: 'pending_upload' },
    }));
    expect(mockUpload).toHaveBeenCalledTimes(1);
  });

  it('keeps an unknown document state uncertain instead of failing it', async () => {
    mockGet.mockResolvedValueOnce(documentBody({
      id: 'doc-9',
      state: 'teleporting' as unknown as KnowledgeDocument['state'],
    }));

    const { result } = renderHook(() => useUploads());
    await act(async () => {
      result.current.resumeDocument(documentBody({ id: 'doc-9' }), pdfFile());
    });

    await waitFor(() => expect(result.current.tasks[0]).toMatchObject({
      phase: 'uncertain',
      errorCode: 'document.invalid_response',
    }));
    expect(mockUpload).not.toHaveBeenCalled();
  });

  it('keeps unsupported and deleted states failed without a PUT', async () => {
    mockCreate.mockResolvedValueOnce(documentBody({
      id: 'doc-1',
      state: 'unsupported',
      error_code: 'document.unsupported',
    }));
    mockGet.mockResolvedValueOnce(documentBody({ id: 'doc-9', state: 'deleted' }));

    const { result } = renderHook(() => useUploads());
    await act(async () => {
      result.current.addFiles('kb-1', [pdfFile()]);
    });
    await waitFor(() => expect(result.current.tasks[0]).toMatchObject({
      phase: 'failed',
      errorCode: 'document.unsupported',
    }));

    await act(async () => {
      result.current.resumeDocument(documentBody({ id: 'doc-9' }), pdfFile());
    });
    await waitFor(() => expect(result.current.tasks[1]).toMatchObject({
      phase: 'failed',
      errorCode: 'document.deleted',
    }));
    expect(mockUpload).not.toHaveBeenCalled();
  });

  it('reconciles a queued document from metadata without re-sending bytes', async () => {
    mockCreate.mockResolvedValueOnce(documentBody({ id: 'doc-1', state: 'queued' }));

    const { result } = renderHook(() => useUploads());
    await act(async () => {
      result.current.addFiles('kb-1', [pdfFile()]);
    });

    await waitFor(() => expect(result.current.tasks[0].phase).toBe('uploaded'));
    expect(mockUpload).not.toHaveBeenCalled();
    expect(result.current.tasks[0].document?.state).toBe('queued');
  });
});

describe('useUploads pause and recovery', () => {
  it('stops dispatching new waiting files on upload_busy and resumes explicitly', async () => {
    mockCreate.mockRejectedValueOnce(new ApiError('Another upload is already in progress.', {
      code: 'document.upload_busy',
      status: 429,
    }));
    const second = deferred<KnowledgeDocument>();
    const third = deferred<KnowledgeDocument>();
    stubCreate(second);
    stubCreate(third);

    const { result } = renderHook(() => useUploads());
    await act(async () => {
      result.current.addFiles('kb-1', [pdfFile('a.pdf'), pdfFile('b.pdf'), pdfFile('c.pdf')]);
    });

    await waitFor(() => expect(result.current.paused).toBe(true));
    expect(mockCreate).toHaveBeenCalledTimes(2);
    expect(result.current.tasks[0]).toMatchObject({ phase: 'failed', errorCode: 'document.upload_busy' });
    expect(result.current.tasks[2].phase).toBe('waiting');

    await act(async () => {
      result.current.resume();
    });

    expect(result.current.paused).toBe(false);
    expect(mockCreate).toHaveBeenCalledTimes(3);
    expect(mockCreate.mock.calls[2][1].filename).toBe('c.pdf');
    expect(result.current.tasks[2].phase).toBe('registering');

    await act(async () => {
      second.resolve(documentBody({ id: 'doc-b', filename: 'b.pdf', state: 'queued' }));
      third.resolve(documentBody({ id: 'doc-c', filename: 'c.pdf', state: 'queued' }));
    });
  });

  it('treats a 503 persistence_failed as uncertain and reconciles before retrying', async () => {
    mockCreate.mockResolvedValueOnce(documentBody({ id: 'doc-1', state: 'pending_upload' }));
    const pending = deferred<KnowledgeDocument>();
    stubUpload(pending);
    mockGet.mockResolvedValueOnce(documentBody({ id: 'doc-1', state: 'processing' }));

    const { result } = renderHook(() => useUploads());
    await act(async () => {
      result.current.addFiles('kb-1', [pdfFile()]);
    });
    await waitFor(() => expect(result.current.tasks[0].phase).toBe('uploading'));

    await act(async () => {
      pending.reject(new ApiError('Upload failed.', {
        code: 'document.persistence_failed',
        status: 503,
      }));
    });

    await waitFor(() => expect(result.current.tasks[0]).toMatchObject({
      phase: 'uncertain',
      errorCode: 'document.persistence_failed',
    }));
    expect(result.current.paused).toBe(true);

    await act(async () => {
      result.current.retry(result.current.tasks[0].id);
    });

    await waitFor(() => expect(result.current.tasks[0].phase).toBe('uploaded'));
    expect(mockGet).toHaveBeenCalledWith('doc-1', expect.anything());
    expect(mockUpload).toHaveBeenCalledTimes(1);
  });

  it('treats a gateway 5xx without a server code as uncertain but an explicit code as failed', async () => {
    mockCreate
      .mockRejectedValueOnce(new ApiError('Bad gateway', { status: 502 }))
      .mockRejectedValueOnce(new ApiError('Upload failed', { code: 'document.upload_failed', status: 500 }));

    const { result } = renderHook(() => useUploads());
    await act(async () => {
      result.current.addFiles('kb-1', [pdfFile('a.pdf'), pdfFile('b.pdf')]);
    });

    await waitFor(() => expect(result.current.tasks[1]).toMatchObject({
      phase: 'failed',
      errorCode: 'document.upload_failed',
    }));
    expect(result.current.tasks[0]).toMatchObject({
      phase: 'uncertain',
      errorCode: 'document.server_error',
    });
    expect(result.current.paused).toBe(true);
    expect(mockUpload).not.toHaveBeenCalled();
  });

  it('treats a coded 5xx other than upload_failed as unconfirmed', async () => {
    mockCreate.mockRejectedValueOnce(new ApiError('Internal error', {
      code: 'internal_error',
      status: 500,
    }));

    const { result } = renderHook(() => useUploads());
    await act(async () => {
      result.current.addFiles('kb-1', [pdfFile()]);
    });

    // A code on the response only names the fault; it does not prove the
    // create never committed, so the row stays unconfirmed.
    await waitFor(() => expect(result.current.tasks[0]).toMatchObject({
      phase: 'uncertain',
      errorCode: 'internal_error',
    }));
    expect(result.current.paused).toBe(true);
    expect(result.current.tasks[0].document).toBeUndefined();
    expect(mockCreate).toHaveBeenCalledTimes(1);
    expect(mockUpload).not.toHaveBeenCalled();
  });

  it('marks a transport failure uncertain, pauses and never retries on its own', async () => {
    mockCreate.mockResolvedValueOnce(documentBody({ id: 'doc-1', state: 'pending_upload' }));
    const pending = deferred<KnowledgeDocument>();
    stubUpload(pending);

    const { result } = renderHook(() => useUploads());
    await act(async () => {
      result.current.addFiles('kb-1', [pdfFile()]);
    });
    await waitFor(() => expect(result.current.tasks[0].phase).toBe('uploading'));

    await act(async () => {
      pending.reject(new ApiError('Network error during upload.', { code: 'NETWORK' }));
    });

    await waitFor(() => expect(result.current.paused).toBe(true));
    expect(result.current.tasks[0]).toMatchObject({ phase: 'uncertain', errorCode: 'NETWORK' });
    expect(mockUpload).toHaveBeenCalledTimes(1);
    expect(mockCreate).toHaveBeenCalledTimes(1);
    expect(mockGet).not.toHaveBeenCalled();
    expect(mockRetry).not.toHaveBeenCalled();
  });
});

describe('useUploads retry', () => {
  it('reconciles a retried task to uploaded when the server is already past the upload', async () => {
    mockCreate.mockResolvedValueOnce(documentBody({ id: 'doc-1', state: 'pending_upload' }));
    const pending = deferred<KnowledgeDocument>();
    stubUpload(pending);
    mockGet.mockResolvedValueOnce(documentBody({ id: 'doc-1', state: 'processing' }));

    const { result } = renderHook(() => useUploads());
    await act(async () => {
      result.current.addFiles('kb-1', [pdfFile()]);
    });
    await waitFor(() => expect(result.current.tasks[0].phase).toBe('uploading'));
    await act(async () => {
      pending.reject(new ApiError('Upload timed out.', { code: 'TIMEOUT' }));
    });
    await waitFor(() => expect(result.current.tasks[0].phase).toBe('uncertain'));

    await act(async () => {
      result.current.retry(result.current.tasks[0].id);
    });

    await waitFor(() => expect(result.current.tasks[0].phase).toBe('uploaded'));
    expect(mockGet).toHaveBeenCalledWith('doc-1', expect.anything());
    expect(mockUpload).toHaveBeenCalledTimes(1);
    expect(result.current.paused).toBe(false);
  });

  it('keeps uncertain without re-sending while the server reports uploading', async () => {
    mockCreate.mockResolvedValueOnce(documentBody({ id: 'doc-1', state: 'failed' }));
    mockGet.mockResolvedValueOnce(documentBody({ id: 'doc-1', state: 'uploading' }));

    const { result } = renderHook(() => useUploads());
    await act(async () => {
      result.current.addFiles('kb-1', [pdfFile()]);
    });
    await waitFor(() => expect(result.current.tasks[0].phase).toBe('failed'));

    await act(async () => {
      result.current.retry(result.current.tasks[0].id);
    });

    await waitFor(() => expect(result.current.tasks[0]).toMatchObject({
      phase: 'uncertain',
      document: { state: 'uploading' },
    }));
    expect(mockUpload).not.toHaveBeenCalled();
    expect(mockRetry).not.toHaveBeenCalled();
  });

  it('does not revive a document the server reports as missing', async () => {
    mockCreate.mockResolvedValueOnce(documentBody({ id: 'doc-1', state: 'failed' }));
    mockGet.mockRejectedValueOnce(new ApiError('Document not found.', {
      code: 'document.not_found',
      status: 404,
    }));

    const { result } = renderHook(() => useUploads());
    await act(async () => {
      result.current.addFiles('kb-1', [pdfFile()]);
    });
    await waitFor(() => expect(result.current.tasks[0].phase).toBe('failed'));

    await act(async () => {
      result.current.retry(result.current.tasks[0].id);
    });

    await waitFor(() => expect(result.current.tasks[0]).toMatchObject({
      phase: 'failed',
      errorCode: 'document.not_found',
    }));
    expect(mockCreate).toHaveBeenCalledTimes(1);
    expect(mockRetry).not.toHaveBeenCalled();
    expect(mockUpload).not.toHaveBeenCalled();
  });

  it('PUTs again only when the retry endpoint returns pending_upload', async () => {
    mockCreate.mockResolvedValueOnce(documentBody({ id: 'doc-1', state: 'failed', error_code: 'document.parse_failed' }));
    mockGet.mockResolvedValueOnce(documentBody({ id: 'doc-1', state: 'failed', error_code: 'document.parse_failed' }));
    mockRetry.mockResolvedValueOnce(documentBody({ id: 'doc-1', state: 'pending_upload' }));
    mockUpload.mockResolvedValueOnce(documentBody({ id: 'doc-1', state: 'queued' }));

    const { result } = renderHook(() => useUploads());
    await act(async () => {
      result.current.addFiles('kb-1', [pdfFile()]);
    });
    await waitFor(() => expect(result.current.tasks[0]).toMatchObject({
      phase: 'failed',
      errorCode: 'document.parse_failed',
    }));

    await act(async () => {
      result.current.retry(result.current.tasks[0].id);
    });

    await waitFor(() => expect(result.current.tasks[0].phase).toBe('uploaded'));
    expect(mockGet).toHaveBeenCalledWith('doc-1', expect.anything());
    expect(mockRetry).toHaveBeenCalledWith('doc-1', expect.anything());
    expect(mockUpload).toHaveBeenCalledTimes(1);
  });

  it('does not PUT when the retry endpoint returns queued', async () => {
    mockCreate.mockResolvedValueOnce(documentBody({ id: 'doc-1', state: 'failed' }));
    mockGet.mockResolvedValueOnce(documentBody({ id: 'doc-1', state: 'failed' }));
    mockRetry.mockResolvedValueOnce(documentBody({ id: 'doc-1', state: 'queued' }));

    const { result } = renderHook(() => useUploads());
    await act(async () => {
      result.current.addFiles('kb-1', [pdfFile()]);
    });
    await waitFor(() => expect(result.current.tasks[0].phase).toBe('failed'));

    await act(async () => {
      result.current.retry(result.current.tasks[0].id);
    });

    await waitFor(() => expect(result.current.tasks[0].phase).toBe('uploaded'));
    expect(mockUpload).not.toHaveBeenCalled();
  });
});

describe('useUploads resumeDocument', () => {
  it('reconciles a matching existing record without creating a new document', async () => {
    mockGet.mockResolvedValueOnce(documentBody({ id: 'doc-9', state: 'pending_upload' }));
    const pending = deferred<KnowledgeDocument>();
    stubUpload(pending);

    const { result } = renderHook(() => useUploads());
    await act(async () => {
      result.current.resumeDocument(documentBody({ id: 'doc-9', state: 'pending_upload' }), pdfFile());
    });

    await waitFor(() => expect(mockGet).toHaveBeenCalledWith('doc-9', expect.anything()));
    expect(mockCreate).not.toHaveBeenCalled();

    await act(async () => {
      pending.resolve(documentBody({ id: 'doc-9', state: 'ready' }));
    });

    await waitFor(() => expect(result.current.tasks[0].phase).toBe('uploaded'));
    expect(mockUpload).toHaveBeenCalledWith('doc-9', expect.any(File), 'application/pdf', expect.anything());
  });

  it('dedupes two rapid resumes of the same document into one task', async () => {
    const meta = deferred<KnowledgeDocument>();
    mockGet.mockReturnValueOnce(meta.promise);
    const pending = deferred<KnowledgeDocument>();
    stubUpload(pending);
    const record = documentBody({ id: 'doc-9', state: 'pending_upload' });

    const { result } = renderHook(() => useUploads());
    await act(async () => {
      result.current.resumeDocument(record, pdfFile());
      result.current.resumeDocument(record, pdfFile());
    });

    // The second call must neither queue a second row nor spend a second slot.
    expect(result.current.tasks).toHaveLength(1);
    expect(mockGet).toHaveBeenCalledTimes(1);
    expect(mockCreate).not.toHaveBeenCalled();

    await act(async () => {
      meta.resolve(documentBody({ id: 'doc-9', state: 'pending_upload' }));
    });
    await waitFor(() => expect(result.current.tasks[0].phase).toBe('uploading'));
    expect(mockUpload).toHaveBeenCalledTimes(1);

    await act(async () => {
      pending.resolve(documentBody({ id: 'doc-9', state: 'ready' }));
    });
    await waitFor(() => expect(result.current.tasks[0].phase).toBe('uploaded'));
    expect(mockUpload).toHaveBeenCalledTimes(1);
    expect(result.current.tasks).toHaveLength(1);
  });

  it('reuses a settled task when the same document is resumed again', async () => {
    mockGet.mockResolvedValueOnce(documentBody({ id: 'doc-9', state: 'ready' }));
    const record = documentBody({ id: 'doc-9', state: 'ready' });

    const { result } = renderHook(() => useUploads());
    await act(async () => {
      result.current.resumeDocument(record, pdfFile());
    });
    await waitFor(() => expect(result.current.tasks[0].phase).toBe('uploaded'));
    const originalId = result.current.tasks[0].id;
    expect(mockUpload).not.toHaveBeenCalled();

    // The server moved back to pending_upload; re-selecting the file refreshes
    // the existing row instead of appending a duplicate record.
    mockGet.mockResolvedValueOnce(documentBody({ id: 'doc-9', state: 'pending_upload' }));
    mockUpload.mockResolvedValueOnce(documentBody({ id: 'doc-9', state: 'queued' }));

    await act(async () => {
      result.current.resumeDocument(record, pdfFile());
    });

    expect(result.current.tasks).toHaveLength(1);
    expect(result.current.tasks[0].id).toBe(originalId);

    await waitFor(() => expect(mockGet).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(result.current.tasks[0].phase).toBe('uploaded'));
    expect(mockUpload).toHaveBeenCalledTimes(1);
    expect(mockCreate).not.toHaveBeenCalled();
  });

  it('fails a mismatched file instead of slipping it into the old record', () => {
    const { result } = renderHook(() => useUploads());

    act(() => {
      result.current.resumeDocument(
        documentBody({ id: 'doc-9', filename: 'guide.pdf', mime_type: 'application/pdf' }),
        new File(['hello'], 'guide.txt', { type: 'text/plain' }),
      );
    });

    expect(result.current.tasks[0]).toMatchObject({
      phase: 'failed',
      errorCode: 'document.file_mismatch',
      document: { id: 'doc-9' },
    });
    expect(mockGet).not.toHaveBeenCalled();
    expect(mockCreate).not.toHaveBeenCalled();
  });
});

describe('useUploads cleanup', () => {
  it('ignores all late results after unmount', async () => {
    const created = deferred<KnowledgeDocument>();
    const create = stubCreate(created);

    const { result, unmount } = renderHook(() => useUploads());
    await act(async () => {
      result.current.addFiles('kb-1', [pdfFile()]);
    });
    expect(create.signal()?.aborted).toBe(false);

    unmount();
    expect(create.signal()?.aborted).toBe(true);

    await act(async () => {
      created.resolve(documentBody({ id: 'doc-1', state: 'pending_upload' }));
    });

    expect(mockUpload).not.toHaveBeenCalled();
    expect(mockDelete).not.toHaveBeenCalled();
  });

  it('aborts the raw upload on unmount and drops the late acknowledgement', async () => {
    mockCreate.mockResolvedValueOnce(documentBody({ id: 'doc-1', state: 'pending_upload' }));
    const pending = deferred<KnowledgeDocument>();
    const upload = stubUpload(pending);

    const { result, unmount } = renderHook(() => useUploads());
    await act(async () => {
      result.current.addFiles('kb-1', [pdfFile()]);
    });
    await waitFor(() => expect(result.current.tasks[0].phase).toBe('uploading'));

    unmount();
    expect(upload.signal()?.aborted).toBe(true);

    // Late progress and a late ack must not touch an unmounted instance.
    await act(async () => {
      upload.emitProgress(8, 8);
      pending.resolve(documentBody({ id: 'doc-1', state: 'ready' }));
    });

    expect(mockDelete).not.toHaveBeenCalled();
  });

  it('holds back the third file on unmount and ignores late metadata, ack and progress', async () => {
    const firstMeta = deferred<KnowledgeDocument>();
    const secondMeta = deferred<KnowledgeDocument>();
    stubCreate(firstMeta);
    stubCreate(secondMeta);
    const pendingAck = deferred<KnowledgeDocument>();
    const upload = stubUpload(pendingAck);

    const { result, unmount } = renderHook(() => useUploads());
    await act(async () => {
      result.current.addFiles('kb-1', [pdfFile('a.pdf'), pdfFile('b.pdf'), pdfFile('c.pdf')]);
    });
    expect(mockCreate).toHaveBeenCalledTimes(2);
    expect(result.current.tasks[2].phase).toBe('waiting');

    // The first file reaches the upload stage; the third still needs a slot.
    await act(async () => {
      firstMeta.resolve(documentBody({ id: 'doc-a', filename: 'a.pdf', state: 'pending_upload' }));
    });
    await waitFor(() => expect(mockUpload).toHaveBeenCalledTimes(1));
    expect(mockCreate).toHaveBeenCalledTimes(2);

    unmount();
    expect(upload.signal()?.aborted).toBe(true);
    expect(result.current.tasks).toHaveLength(3);

    // Late results of the dead generation must not dispatch the third file or PUT again.
    await act(async () => {
      upload.emitProgress(8, 8);
      pendingAck.resolve(documentBody({ id: 'doc-a', filename: 'a.pdf', state: 'ready' }));
      secondMeta.resolve(documentBody({ id: 'doc-b', filename: 'b.pdf', state: 'queued' }));
    });

    expect(mockCreate).toHaveBeenCalledTimes(2);
    expect(mockUpload).toHaveBeenCalledTimes(1);
    expect(mockGet).not.toHaveBeenCalled();
  });

  it('survives the StrictMode effect replay without aborting or duplicating work', async () => {
    const created = deferred<KnowledgeDocument>();
    const create = stubCreate(created);
    const wrapper = ({ children }: { children: ReactNode }) => <StrictMode>{children}</StrictMode>;

    const { result } = renderHook(() => useUploads(), { wrapper });
    expect(create.signal()).toBeUndefined();

    await act(async () => {
      result.current.addFiles('kb-1', [pdfFile()]);
    });

    expect(mockCreate).toHaveBeenCalledTimes(1);
    expect(create.signal()?.aborted).toBe(false);

    await act(async () => {
      created.resolve(documentBody({ id: 'doc-1', state: 'queued' }));
    });

    await waitFor(() => expect(result.current.tasks[0].phase).toBe('uploaded'));
    expect(mockUpload).not.toHaveBeenCalled();
  });
});

describe('useUploads remove', () => {
  it('removes only inactive local rows and never deletes a server document', async () => {
    mockCreate.mockResolvedValueOnce(documentBody({ id: 'doc-1', state: 'pending_upload' }));
    const pending = deferred<KnowledgeDocument>();
    stubUpload(pending);

    const { result } = renderHook(() => useUploads());
    await act(async () => {
      result.current.addFiles('kb-1', [pdfFile()]);
    });
    await waitFor(() => expect(result.current.tasks[0].phase).toBe('uploading'));

    const activeId = result.current.tasks[0].id;
    act(() => {
      result.current.remove(activeId);
    });
    expect(result.current.tasks).toHaveLength(1);

    await act(async () => {
      pending.resolve(documentBody({ id: 'doc-1', state: 'queued' }));
    });
    await waitFor(() => expect(result.current.tasks[0].phase).toBe('uploaded'));

    act(() => {
      result.current.remove(result.current.tasks[0].id);
    });
    expect(result.current.tasks).toHaveLength(0);
    expect(mockDelete).not.toHaveBeenCalled();
  });
});
