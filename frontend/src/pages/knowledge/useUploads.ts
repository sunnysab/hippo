import { useEffect, useRef, useState } from 'react';
import { ApiError } from '../../api/index';
import {
  createDocument,
  getDocument,
  retryDocument,
  uploadDocumentFile,
} from '../../api/knowledge';
import type { DocumentMime, KnowledgeDocument } from '../../api/knowledge';

export type UploadPhase = 'waiting' | 'registering' | 'uploading' | 'uploaded' | 'failed' | 'uncertain';

export interface UploadTask {
  id: string;
  /** Frozen when the file joined the queue, so switching bases cannot move it. */
  baseId: string;
  /** Kept in memory only; its bytes are never read or persisted here. */
  file: File;
  mime_type: DocumentMime | null;
  phase: UploadPhase;
  loaded: number;
  total: number;
  /** The last document the server confirmed, real state included. */
  document?: KnowledgeDocument;
  errorCode?: string;
}

export interface UseUploadsResult {
  tasks: UploadTask[];
  paused: boolean;
  addFiles: (baseId: string, files: readonly File[]) => void;
  resumeDocument: (document: KnowledgeDocument, file: File) => void;
  retry: (taskId: string) => void;
  remove: (taskId: string) => void;
  resume: () => void;
}

/** One metadata round trip plus its raw upload counts as one local slot. */
const MAX_CONCURRENT = 2;
const MAX_UPLOAD_BYTES = 64 * 1024 * 1024;

/** The one code that proves the server rejected the upload itself. */
const UPLOAD_REJECTED_CODE = 'document.upload_failed';
/** Codes that stop dispatching waiting files instead of registering rows for all of them. */
const PAUSED_CODES = new Set(['document.upload_busy', 'AUTH_REQUIRED', 'NETWORK', 'TIMEOUT']);
const INVALID_RESPONSE_CODE = 'document.invalid_response';
const REQUEST_FAILED_CODE = 'document.request_failed';
const SERVER_ERROR_CODE = 'document.server_error';
const FILE_MISMATCH_CODE = 'document.file_mismatch';
const NETWORK_CODE = 'NETWORK';

/** The server stores the extension's canonical type; an empty browser File.type is ignored. */
const mimeFromName = (name: string): DocumentMime | null => {
  const lower = name.toLowerCase();
  if (lower.endsWith('.pdf')) return 'application/pdf';
  if (lower.endsWith('.txt')) return 'text/plain';
  return null;
};

type Validation = { mime: DocumentMime } | { errorCode: string };

const validateFile = (file: File): Validation => {
  const mime = mimeFromName(file.name);
  if (!mime) return { errorCode: 'document.unsupported' };
  if (file.size === 0) return { errorCode: 'document.empty' };
  if (file.size > MAX_UPLOAD_BYTES) return { errorCode: 'document.too_large' };
  return { mime };
};

/** Metadata may answer without an id, state or base; such a row must never drive a PUT. */
const isUsableDocument = (value: unknown): value is KnowledgeDocument => {
  if (typeof value !== 'object' || value === null) return false;
  const document = value as Partial<KnowledgeDocument>;
  return typeof document.id === 'string' && document.id !== ''
    && typeof document.knowledge_base_id === 'string' && document.knowledge_base_id !== ''
    && typeof document.state === 'string';
};

interface Outcome {
  phase: 'failed' | 'uncertain';
  errorCode: string;
  pause: boolean;
}

/** ``null`` means the request was aborted by this hook and must change nothing. */
const classifyError = (error: unknown): Outcome | null => {
  if ((error as { name?: string } | null)?.name === 'AbortError') return null;
  if (error instanceof ApiError) {
    const code = error.code ?? '';
    if (PAUSED_CODES.has(code)) {
      const rejected = code === 'document.upload_busy' || code === 'AUTH_REQUIRED';
      return { phase: rejected ? 'failed' : 'uncertain', errorCode: code, pause: true };
    }
    if (typeof error.status === 'number') {
      // Only ``document.upload_failed`` is proof that the upload was rejected.
      // Every other 5xx -- coded, uncoded or a gateway error -- may still have
      // committed, so it stays uncertain and pauses new dispatches; a code
      // being present is not the same as the outcome being known.
      if (error.status >= 500 && code !== UPLOAD_REJECTED_CODE) {
        return { phase: 'uncertain', errorCode: code || SERVER_ERROR_CODE, pause: true };
      }
      return { phase: 'failed', errorCode: code || REQUEST_FAILED_CODE, pause: false };
    }
    // A 2xx body we could not read says nothing about the server-side job.
    return { phase: 'uncertain', errorCode: code || INVALID_RESPONSE_CODE, pause: false };
  }
  if (error instanceof SyntaxError) {
    return { phase: 'uncertain', errorCode: INVALID_RESPONSE_CODE, pause: false };
  }
  // A bare transport rejection: the request may or may not have reached the server.
  return { phase: 'uncertain', errorCode: NETWORK_CODE, pause: true };
};

export function useUploads(): UseUploadsResult {
  const [tasks, setTasks] = useState<UploadTask[]>([]);
  const [paused, setPaused] = useState(false);

  const tasksRef = useRef<UploadTask[]>([]);
  const pausedRef = useRef(false);
  const controllersRef = useRef(new Map<string, AbortController>());
  const generationRef = useRef(0);
  const counterRef = useRef(0);

  useEffect(() => () => {
    // Invalidate every callback born in this generation (StrictMode remount or a
    // real unmount) before aborting the requests it owns.
    generationRef.current += 1;
    controllersRef.current.forEach((controller) => controller.abort());
    controllersRef.current.clear();
  }, []);

  const current = (generation: number) => generation === generationRef.current;

  const commit = (updater: (previous: UploadTask[]) => UploadTask[]) => {
    tasksRef.current = updater(tasksRef.current);
    setTasks(tasksRef.current);
  };

  const patch = (id: string, changes: Partial<UploadTask>) => (previous: UploadTask[]) =>
    previous.map((task) => (task.id === id ? { ...task, ...changes } : task));

  const updatePaused = (value: boolean) => {
    pausedRef.current = value;
    setPaused(value);
  };

  function nextId() {
    counterRef.current += 1;
    return `upload-${counterRef.current}`;
  }

  function newTask(
    baseId: string,
    file: File,
    phase: UploadPhase,
    fields: { mime_type?: DocumentMime; document?: KnowledgeDocument; errorCode?: string } = {},
  ): UploadTask {
    return {
      id: nextId(),
      baseId,
      file,
      mime_type: fields.mime_type ?? null,
      phase,
      loaded: 0,
      total: file.size,
      document: fields.document,
      errorCode: fields.errorCode,
    };
  }

  function isActivePhase(phase: UploadPhase) {
    return phase === 'registering' || phase === 'uploading';
  }

  function activeCount() {
    return tasksRef.current.filter((task) => isActivePhase(task.phase)).length;
  }

  function pump() {
    if (pausedRef.current) return;
    let running = activeCount();
    // ``run`` rewrites the row synchronously, so re-read the list on each step.
    // ``dispatched`` is only a guard against a row that stayed ``waiting``.
    const dispatched = new Set<string>();
    while (running < MAX_CONCURRENT) {
      const next = tasksRef.current.find(
        (task) => task.phase === 'waiting' && !dispatched.has(task.id),
      );
      if (!next) return;
      dispatched.add(next.id);
      running += 1;
      void run(next.id);
    }
  }

  function settleFailure(taskId: string, generation: number, error: unknown) {
    if (!current(generation)) return;
    const outcome = classifyError(error);
    if (!outcome) return;
    if (outcome.pause) updatePaused(true);
    commit(patch(taskId, { phase: outcome.phase, errorCode: outcome.errorCode }));
  }

  async function run(taskId: string): Promise<void> {
    const generation = generationRef.current;
    const controller = new AbortController();
    controllersRef.current.set(taskId, controller);
    try {
      const task = tasksRef.current.find((item) => item.id === taskId);
      if (!task) return;
      const mime = task.mime_type;
      if (!mime) {
        commit(patch(taskId, { phase: 'failed', errorCode: task.errorCode ?? 'document.unsupported' }));
        return;
      }
      if (task.document) await reconcile(task, mime, generation, controller);
      else await register(task, mime, generation, controller);
    } catch (error) {
      settleFailure(taskId, generation, error);
    } finally {
      controllersRef.current.delete(taskId);
      if (current(generation)) pump();
    }
  }

  async function register(task: UploadTask, mime: DocumentMime, generation: number, controller: AbortController) {
    commit(patch(task.id, { phase: 'registering', loaded: 0, total: task.file.size, errorCode: undefined }));
    const created = await createDocument(
      task.baseId,
      { filename: task.file.name, mime_type: mime },
      { signal: controller.signal },
    );
    if (!current(generation)) return;
    if (!isUsableDocument(created)) {
      commit(patch(task.id, { phase: 'uncertain', errorCode: INVALID_RESPONSE_CODE }));
      return;
    }
    commit(patch(task.id, { document: created }));
    await proceed(task, created, mime, generation, controller, false);
  }

  async function reconcile(task: UploadTask, mime: DocumentMime, generation: number, controller: AbortController) {
    const known = task.document;
    if (!known) return;
    commit(patch(task.id, { phase: 'registering', errorCode: undefined }));
    const latest = await getDocument(known.id, { signal: controller.signal });
    if (!current(generation)) return;
    if (!isUsableDocument(latest)) {
      commit(patch(task.id, { phase: 'uncertain', errorCode: INVALID_RESPONSE_CODE }));
      return;
    }
    commit(patch(task.id, { document: latest }));
    await proceed(task, latest, mime, generation, controller, true);
  }

  async function proceed(
    task: UploadTask,
    document: KnowledgeDocument,
    mime: DocumentMime,
    generation: number,
    controller: AbortController,
    allowRetry: boolean,
  ) {
    switch (document.state) {
      case 'queued':
      case 'processing':
      case 'ready':
        commit(patch(task.id, { phase: 'uploaded', document }));
        return;
      case 'pending_upload':
        await upload(task, document, mime, generation, controller);
        return;
      case 'uploading':
        // Someone else owns the bytes; a blind PUT here could corrupt them.
        commit(patch(task.id, { phase: 'uncertain', document }));
        return;
      case 'unsupported':
      case 'deleted':
        commit(patch(task.id, { phase: 'failed', document, errorCode: document.error_code ?? `document.${document.state}` }));
        return;
      case 'failed': {
        if (!allowRetry) {
          commit(patch(task.id, { phase: 'failed', document, errorCode: document.error_code ?? 'document.failed' }));
          return;
        }
        const retried = await retryDocument(document.id, { signal: controller.signal });
        if (!current(generation)) return;
        if (!isUsableDocument(retried)) {
          commit(patch(task.id, { phase: 'uncertain', errorCode: INVALID_RESPONSE_CODE }));
          return;
        }
        commit(patch(task.id, { document: retried }));
        await proceed(task, retried, mime, generation, controller, false);
        return;
      }
      default:
        // An unknown state is not a rejection: stay uncertain and never PUT.
        commit(patch(task.id, { phase: 'uncertain', document, errorCode: INVALID_RESPONSE_CODE }));
    }
  }

  async function upload(
    task: UploadTask,
    document: KnowledgeDocument,
    mime: DocumentMime,
    generation: number,
    controller: AbortController,
  ) {
    commit(patch(task.id, { phase: 'uploading', loaded: 0, total: task.file.size, errorCode: undefined }));
    const uploaded = await uploadDocumentFile(document.id, task.file, mime, {
      signal: controller.signal,
      onProgress: ({ loaded, total }) => {
        if (!current(generation)) return;
        commit(patch(task.id, { loaded, total }));
      },
    });
    if (!current(generation)) return;
    if (!isUsableDocument(uploaded)) {
      // Client-side 100% is not an ack; without a document the task stays unknown.
      commit(patch(task.id, { phase: 'uncertain', errorCode: INVALID_RESPONSE_CODE }));
      return;
    }
    if (uploaded.state === 'queued' || uploaded.state === 'processing' || uploaded.state === 'ready') {
      commit(patch(task.id, {
        phase: 'uploaded',
        document: uploaded,
        loaded: task.file.size,
        total: task.file.size,
      }));
      return;
    }
    if (uploaded.state === 'failed' || uploaded.state === 'unsupported' || uploaded.state === 'deleted') {
      commit(patch(task.id, {
        phase: 'failed',
        document: uploaded,
        errorCode: uploaded.error_code ?? `document.${uploaded.state}`,
      }));
      return;
    }
    // An ack that still says pending_upload/uploading means the bytes were not
    // accepted; never treat that as success and never PUT in a loop.
    commit(patch(task.id, {
      phase: 'uncertain',
      document: uploaded,
      errorCode: uploaded.error_code ?? INVALID_RESPONSE_CODE,
    }));
  }

  const addFiles = (baseId: string, files: readonly File[]) => {
    const added = files.map((file) => {
      const validation = validateFile(file);
      if ('errorCode' in validation) return newTask(baseId, file, 'failed', { errorCode: validation.errorCode });
      return newTask(baseId, file, 'waiting', { mime_type: validation.mime });
    });
    if (added.length === 0) return;
    commit((previous) => [...previous, ...added]);
    pump();
  };

  const resumeDocument = (document: KnowledgeDocument, file: File) => {
    const validation = validateFile(file);
    if ('errorCode' in validation) {
      commit((previous) => [...previous, newTask(document.knowledge_base_id, file, 'failed', {
        errorCode: validation.errorCode,
        document,
      })]);
      return;
    }
    if (document.filename !== file.name || document.mime_type !== validation.mime) {
      // Never let a different file silently take over an existing record.
      commit((previous) => [...previous, newTask(document.knowledge_base_id, file, 'failed', {
        errorCode: FILE_MISMATCH_CODE,
        document,
      })]);
      return;
    }
    const existing = tasksRef.current.find((task) => task.document?.id === document.id);
    // One document id owns at most one queued or in-flight row, so a second
    // resume can never spawn a parallel PUT for the same bytes.
    if (existing && (existing.phase === 'waiting' || isActivePhase(existing.phase))) return;
    if (existing) {
      // Re-selecting the file for a settled record reuses the row; ``run`` still
      // reconciles with ``getDocument`` first because the document is kept.
      commit(patch(existing.id, {
        file,
        mime_type: validation.mime,
        document,
        phase: 'waiting',
        loaded: 0,
        total: file.size,
        errorCode: undefined,
      }));
      pump();
      return;
    }
    commit((previous) => [...previous, newTask(document.knowledge_base_id, file, 'waiting', {
      mime_type: validation.mime,
      document,
    })]);
    pump();
  };

  const retry = (taskId: string) => {
    const task = tasksRef.current.find((item) => item.id === taskId);
    // A file that never validated (or never matched its record) cannot be retried:
    // the File itself is the problem, so no request is made.
    if (!task || !task.mime_type) return;
    if (isActivePhase(task.phase)) return;
    updatePaused(false);
    commit(patch(taskId, { phase: 'waiting', errorCode: undefined }));
    pump();
  };

  const remove = (taskId: string) => {
    const task = tasksRef.current.find((item) => item.id === taskId);
    // Active tasks keep their row; the server document is never deleted here.
    if (!task || isActivePhase(task.phase)) return;
    commit((previous) => previous.filter((item) => item.id !== taskId));
  };

  const resume = () => {
    updatePaused(false);
    pump();
  };

  return { tasks, paused, addFiles, resumeDocument, retry, remove, resume };
}
