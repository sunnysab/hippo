import { ApiError, apiGet, apiSend, handleAuthError } from './index';

export interface KnowledgeBase {
  id: string;
  name: string;
  created_at: string;
  updated_at: string;
}

export type DocumentMime = 'application/pdf' | 'text/plain';

/** The eight server-side states; ``needs_ocr`` is an ``error_code``, not a state. */
export type DocumentState =
  | 'pending_upload'
  | 'uploading'
  | 'queued'
  | 'processing'
  | 'ready'
  | 'failed'
  | 'unsupported'
  | 'deleted';

export interface KnowledgeDocument {
  id: string;
  knowledge_base_id: string;
  filename: string;
  mime_type: DocumentMime | null;
  size_bytes: number | null;
  state: DocumentState;
  attempts: number;
  error_code: string | null;
  error_message: string | null;
  page_count: number | null;
  version: number;
  created_at: string;
  updated_at: string;
}

export interface DocumentChunk {
  page_no: number;
  ordinal: number;
  text: string;
  chunk_type: string;
  heading_level: number | null;
}

export interface DocumentContent {
  document: KnowledgeDocument;
  chunks: DocumentChunk[];
  next_offset: number | null;
}

export interface SearchHit {
  document_id: string;
  knowledge_base_id: string;
  filename: string;
  version: number;
  page_no: number;
  ordinal: number;
  text: string;
  chunk_type: string;
  heading_level: number | null;
}

export interface SearchPage {
  hits: SearchHit[];
  next_offset: number | null;
}

export interface RequestOptions {
  signal?: AbortSignal;
}

export interface PageOptions extends RequestOptions {
  limit?: number;
  offset?: number;
}

export interface ContentOptions extends PageOptions {
  page_no?: number;
}

export interface SearchOptions extends PageOptions {
  document_id?: string;
}

export interface UploadOptions extends RequestOptions {
  /** Client bytes sent, never server-side parsing or indexing progress. */
  onProgress?: (progress: { loaded: number; total: number }) => void;
}

const basePath = (id: string) => `/api/knowledge-base/${encodeURIComponent(id)}`;

const documentPath = (id: string) => `/api/document/${encodeURIComponent(id)}`;

const query = (params: Record<string, string | number | undefined>) => {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined) search.set(key, String(value));
  }
  const encoded = search.toString();
  return encoded ? `?${encoded}` : '';
};

const asJson = (text: string): Record<string, unknown> => {
  try {
    const parsed = JSON.parse(text || '{}') as unknown;
    return typeof parsed === 'object' && parsed !== null && !Array.isArray(parsed)
      ? parsed as Record<string, unknown>
      : {};
  } catch {
    return {};
  }
};

const serverError = (text: string) => {
  const payload = asJson(text);
  return {
    error: typeof payload.error === 'string' ? payload.error : undefined,
    code: typeof payload.code === 'string' ? payload.code : undefined,
  };
};

const resource = <T>(payload: Record<string, unknown>, key: string): T => {
  const value = payload[key];
  if (typeof value !== 'object' || value === null || Array.isArray(value)) {
    throw new ApiError(`Response is missing ${key}.`);
  }
  return value as T;
};

const collection = <T>(payload: Record<string, unknown>, key: string): T[] => {
  const value = payload[key];
  if (!Array.isArray(value)) throw new ApiError(`Response is missing ${key}.`);
  return value as T[];
};

const nextOffset = (payload: Record<string, unknown>) =>
  typeof payload.next_offset === 'number' ? payload.next_offset : null;

export const listKnowledgeBases = async (options: PageOptions = {}): Promise<KnowledgeBase[]> => {
  const payload = await apiGet(
    `/api/knowledge-base${query({ limit: options.limit, offset: options.offset })}`,
    { signal: options.signal },
  );
  return collection<KnowledgeBase>(payload, 'knowledge_bases');
};

export const getKnowledgeBase = async (id: string, options: RequestOptions = {}): Promise<KnowledgeBase> => {
  const payload = await apiGet(basePath(id), { signal: options.signal });
  return resource<KnowledgeBase>(payload, 'knowledge_base');
};

export const createKnowledgeBase = async (name: string, options: RequestOptions = {}): Promise<KnowledgeBase> => {
  const payload = await apiSend('/api/knowledge-base', 'POST', { name }, { signal: options.signal });
  return resource<KnowledgeBase>(payload, 'knowledge_base');
};

export const renameKnowledgeBase = async (
  id: string,
  name: string,
  options: RequestOptions = {},
): Promise<KnowledgeBase> => {
  const payload = await apiSend(basePath(id), 'PATCH', { name }, { signal: options.signal });
  return resource<KnowledgeBase>(payload, 'knowledge_base');
};

export const deleteKnowledgeBase = async (id: string, options: RequestOptions = {}): Promise<void> => {
  await apiSend(basePath(id), 'DELETE', {}, { signal: options.signal });
};

export const listDocuments = async (
  baseId: string,
  options: PageOptions = {},
): Promise<KnowledgeDocument[]> => {
  const payload = await apiGet(
    `${basePath(baseId)}/document${query({ limit: options.limit, offset: options.offset })}`,
    { signal: options.signal },
  );
  return collection<KnowledgeDocument>(payload, 'documents');
};

export const getDocument = async (id: string, options: RequestOptions = {}): Promise<KnowledgeDocument> => {
  const payload = await apiGet(documentPath(id), { signal: options.signal });
  return resource<KnowledgeDocument>(payload, 'document');
};

export const createDocument = async (
  baseId: string,
  input: { filename: string; mime_type: DocumentMime },
  options: RequestOptions = {},
): Promise<KnowledgeDocument> => {
  const payload = await apiSend(
    `${basePath(baseId)}/document`,
    'POST',
    { filename: input.filename, mime_type: input.mime_type },
    { signal: options.signal },
  );
  return resource<KnowledgeDocument>(payload, 'document');
};

export const deleteDocument = async (id: string, options: RequestOptions = {}): Promise<void> => {
  await apiSend(documentPath(id), 'DELETE', {}, { signal: options.signal });
};

/**
 * Ask the server to prepare a retry. A 200 only means the retry was prepared:
 * the document may now be ``pending_upload`` (send the file again) or ``queued``
 * (the import worker resumes on its own), so inspect the returned state instead
 * of assuming a re-upload happened.
 */
export const retryDocument = async (id: string, options: RequestOptions = {}): Promise<KnowledgeDocument> => {
  const payload = await apiSend(`${documentPath(id)}/retry`, 'POST', {}, { signal: options.signal });
  return resource<KnowledgeDocument>(payload, 'document');
};

export const getDocumentContent = async (
  id: string,
  options: ContentOptions = {},
): Promise<DocumentContent> => {
  const payload = await apiGet(
    `${documentPath(id)}/content${query({
      limit: options.limit,
      offset: options.offset,
      page_no: options.page_no,
    })}`,
    { signal: options.signal },
  );
  return {
    document: resource<KnowledgeDocument>(payload, 'document'),
    chunks: collection<DocumentChunk>(payload, 'chunks'),
    next_offset: nextOffset(payload),
  };
};

export const searchKnowledgeBase = async (
  baseId: string,
  q: string,
  options: SearchOptions = {},
): Promise<SearchPage> => {
  const payload = await apiGet(
    `${basePath(baseId)}/search${query({
      q,
      limit: options.limit,
      offset: options.offset,
      document_id: options.document_id,
    })}`,
    { signal: options.signal },
  );
  return {
    hits: collection<SearchHit>(payload, 'hits'),
    next_offset: nextOffset(payload),
  };
};

/** Fixed client-side timeout for one raw upload, in milliseconds. */
const UPLOAD_TIMEOUT_MS = 300000;

/**
 * PUT the document bytes as the request body. ``fetch`` cannot report how much
 * of a request body has been sent, so this one call uses ``XMLHttpRequest``.
 *
 * A client timeout or abort only ends this request; the server-side job keeps
 * running. A later queue must reconcile with ``getDocument`` before deciding
 * whether to send the file again, never retry blindly.
 */
export const uploadDocumentFile = (
  documentId: string,
  file: File,
  mime_type: DocumentMime,
  options: UploadOptions = {},
): Promise<KnowledgeDocument> => {
  const { signal, onProgress } = options;
  return new Promise<KnowledgeDocument>((resolve, reject) => {
    if (signal?.aborted) {
      reject(new DOMException('The upload was aborted.', 'AbortError'));
      return;
    }
    const xhr = new XMLHttpRequest();
    let settled = false;
    let timeoutId: ReturnType<typeof setTimeout> | null = null;

    const cleanup = () => {
      if (timeoutId !== null) {
        clearTimeout(timeoutId);
        timeoutId = null;
      }
      xhr.upload.onprogress = null;
      xhr.onload = null;
      xhr.onerror = null;
      xhr.onabort = null;
      signal?.removeEventListener('abort', abortFromSignal);
    };

    const settle = (error: unknown, document?: KnowledgeDocument) => {
      if (settled) return false;
      settled = true;
      cleanup();
      if (error !== null) reject(error);
      else resolve(document as KnowledgeDocument);
      return true;
    };

    const abortWith = (error: unknown) => {
      if (settle(error)) xhr.abort();
    };

    const abortFromSignal = () => {
      abortWith(new DOMException('The upload was aborted.', 'AbortError'));
    };

    xhr.onload = () => {
      // A captured late handler must not fire auth side effects after this
      // upload already settled (succeed, timeout or abort).
      if (settled) return;
      if (xhr.status >= 200 && xhr.status < 300) {
        try {
          settle(null, resource<KnowledgeDocument>(asJson(xhr.responseText), 'document'));
        } catch (error) {
          settle(error);
        }
        return;
      }
      const { error, code } = serverError(xhr.responseText);
      if (xhr.status === 401) {
        handleAuthError(error);
        settle(new ApiError(error || 'Authentication required', { code: 'AUTH_REQUIRED', status: xhr.status }));
        return;
      }
      settle(new ApiError(error || `Upload failed: ${xhr.status}`, { code, status: xhr.status }));
    };
    xhr.onerror = () => settle(new ApiError('Network error during upload.', { code: 'NETWORK' }));
    xhr.onabort = () => settle(new DOMException('The upload was aborted.', 'AbortError'));

    signal?.addEventListener('abort', abortFromSignal);

    try {
      xhr.open('PUT', `${documentPath(documentId)}/file`, true);
      // The server keeps the row's canonical MIME type; the body is raw bytes,
      // never multipart, base64 or a whole-file read.
      xhr.setRequestHeader('Content-Type', mime_type);
      xhr.setRequestHeader('Accept', 'application/json');
      if (onProgress) {
        xhr.upload.onprogress = (event) => {
          if (settled || !event.lengthComputable) return;
          onProgress({ loaded: event.loaded, total: event.total });
        };
      }
      timeoutId = setTimeout(() => {
        abortWith(new ApiError('Upload timed out.', { code: 'TIMEOUT' }));
      }, UPLOAD_TIMEOUT_MS);
      xhr.send(file);
    } catch (error) {
      settle(error);
    }
  });
};
