import { useEffect, useId, useLayoutEffect, useMemo, useRef, useState, type ChangeEvent, type DragEvent, type FormEvent } from 'react';
import { useLocation, useSearchParams } from 'react-router-dom';
import { ApiError, isAuthError } from '../../api';
import {
  createKnowledgeBase,
  deleteDocument,
  deleteKnowledgeBase,
  getKnowledgeBase,
  listDocuments,
  listKnowledgeBases,
  renameKnowledgeBase,
  retryDocument,
  searchKnowledgeBase,
  type DocumentState,
  type KnowledgeBase,
  type KnowledgeDocument,
  type SearchHit,
} from '../../api/knowledge';
import { ConfirmModal } from '../../components/ConfirmModal';
import { EmptyState } from '../../components/EmptyState';
import { LoadingIndicator } from '../../components/LoadingIndicator';
import { useI18n } from '../../i18n';
import { useToast } from '../../hooks/useToast';
import { DocumentReader } from './DocumentReader';
import { UploadQueue } from './UploadQueue';
import { useUploads } from './useUploads';
import '../../styles/knowledge.css';

/** One page of a metadata list, plus the row that proves another page exists. */
const WINDOW_SIZE = 50;
/** The search endpoint owns its own page size and exact next_offset. */
const SEARCH_PAGE_SIZE = 20;
const POLL_INTERVAL_MS = 5000;
const MAX_NAME_LENGTH = 200;

/** States whose server-side work may still be running, so the window is polled. */
const POLLING_STATES: ReadonlySet<DocumentState> = new Set([
  'pending_upload',
  'uploading',
  'queued',
  'processing',
]);

interface FetchWindow<T> {
  /** The scope that produced this window; a mismatch means "not loaded yet". */
  scope: string;
  status: 'ready' | 'error';
  items: T[];
  hasNext: boolean;
}

interface SearchWindow {
  scope: string;
  status: 'ready' | 'error';
  hits: SearchHit[];
  nextOffset: number | null;
}

interface BaseInfo {
  scope: string;
  /** ``missing`` is a 404; any other failure stays a generic load error. */
  status: 'ready' | 'missing' | 'error';
  base: KnowledgeBase | null;
}

const EMPTY_BASES: FetchWindow<KnowledgeBase> = { scope: '', status: 'error', items: [], hasNext: false };
const EMPTY_DOCUMENTS: FetchWindow<KnowledgeDocument> = { scope: '', status: 'error', items: [], hasNext: false };
const EMPTY_SEARCH: SearchWindow = { scope: '', status: 'error', hits: [], nextOffset: null };
const EMPTY_BASE_INFO: BaseInfo = { scope: '', status: 'error', base: null };

const isAbortError = (error: unknown) => (error as Error | null)?.name === 'AbortError';

/**
 * A transport-level failure or a 5xx says nothing about the data, so the rows
 * already on screen stay the best answer we have. An auth loss or a refusal
 * (AUTH_REQUIRED, 401/403/404 and friends) proves the window is unreadable and
 * must not keep showing rows the server would no longer serve.
 */
const keepsVisibleRows = (error: unknown): boolean => {
  if (!(error instanceof ApiError)) return true;
  if (error.code === 'AUTH_REQUIRED') return false;
  if (error.status === undefined) return true;
  return error.status >= 500;
};

/** A window param is absent or a non-negative integer; anything else is zero. */
const parseOffset = (raw: string | null): number => {
  if (raw === null) return 0;
  const trimmed = raw.trim();
  const value = Number(trimmed);
  return /^\d+$/.test(trimmed) && Number.isSafeInteger(value) ? value : 0;
};

/** The i18n dictionary keeps ``{name}`` placeholders; fill them here. */
const fill = (template: string, values: Record<string, string | number>) =>
  Object.entries(values).reduce(
    (text, [key, value]) => text.replace(`{${key}}`, String(value)),
    template,
  );

const formatBytes = (bytes: number | null): string => {
  if (bytes === null || !Number.isFinite(bytes) || bytes < 0) return '';
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KiB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MiB`;
};

export function KnowledgePage() {
  const { t } = useI18n();
  const { showToast } = useToast();
  const [searchParams, setSearchParams] = useSearchParams();
  const uploads = useUploads();
  const nameInputId = useId();
  const searchInputId = useId();

  const baseId = searchParams.get('base') || null;
  const documentId = searchParams.get('document') || null;
  const query = searchParams.get('q') ?? '';
  const searchQuery = query.trim();
  const basesOffset = parseOffset(searchParams.get('bases_offset'));
  const docsOffset = parseOffset(searchParams.get('docs_offset'));
  const searchOffset = parseOffset(searchParams.get('search_offset'));

  // A reader keeps the page and the upload hook mounted; only the library body
  // swaps between document list, search results and the reader.
  const documentsMode = baseId !== null && documentId === null && searchQuery === '';
  const searchMode = baseId !== null && documentId === null && searchQuery !== '';

  const location = useLocation();
  const navigationKey = location.key;
  const mountedRef = useRef(true);
  // A mutation handler reads these after its await: the render-time values in
  // its closure can be one navigation old, which is how a slow PATCH used to
  // put base A back on screen while the address already said B.
  const latestKeyRef = useRef(navigationKey);
  const latestBaseIdRef = useRef(baseId);
  const latestDocumentsModeRef = useRef(documentsMode);
  useLayoutEffect(() => {
    latestKeyRef.current = navigationKey;
    latestBaseIdRef.current = baseId;
    latestDocumentsModeRef.current = documentsMode;
  });
  useEffect(() => {
    // StrictMode remounts the same instance, so the flag is restored on every
    // effect run instead of only being lowered.
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);
  const inCurrentScope = (key: string) => mountedRef.current && latestKeyRef.current === key;

  const basesScope = `bases:${basesOffset}`;
  const docsScope = `docs:${baseId ?? ''}:${docsOffset}`;
  const searchScope = `search:${baseId ?? ''}:${searchQuery}:${searchOffset}`;

  const [basesState, setBasesState] = useState<FetchWindow<KnowledgeBase>>(EMPTY_BASES);
  const [documentsState, setDocumentsState] = useState<FetchWindow<KnowledgeDocument>>(EMPTY_DOCUMENTS);
  const [searchState, setSearchState] = useState<SearchWindow>(EMPTY_SEARCH);
  const [baseInfo, setBaseInfo] = useState<BaseInfo>(EMPTY_BASE_INFO);
  const [basesToken, setBasesToken] = useState(0);
  const [baseToken, setBaseToken] = useState(0);
  const [documentsToken, setDocumentsToken] = useState(0);

  const [nameDialog, setNameDialog] = useState<
    { mode: 'create' | 'rename'; value: string; baseId: string | null; key: string } | null
  >(null);  const [nameSaving, setNameSaving] = useState(false);
  const [baseToDelete, setBaseToDelete] = useState<{ base: KnowledgeBase; key: string } | null>(null);
  const [documentToDelete, setDocumentToDelete] = useState<{ document: KnowledgeDocument; key: string } | null>(null);
  const [documentToResume, setDocumentToResume] = useState<KnowledgeDocument | null>(null);
  const [retryingId, setRetryingId] = useState<string | null>(null);
  // A window that failed while its previous rows are still on screen keeps a
  // separate flag, so a refresh failure does not hide what the user can see.
  // It stores the window it belongs to, which is also why another scope never
  // shows this banner.
  const [documentsError, setDocumentsError] = useState<string | null>(null);
  const [searchError, setSearchError] = useState<string | null>(null);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [draft, setDraft] = useState<{ key: string; text: string } | null>(null);

  const basesWindow = basesState.scope === basesScope ? basesState : null;
  const documentsWindow = documentsState.scope === docsScope ? documentsState : null;
  const searchWindow = searchState.scope === searchScope ? searchState : null;
  const baseInfoWindow = baseId !== null && baseInfo.scope === baseId ? baseInfo : null;
  // The base named by the address is only usable once the server confirmed it:
  // a dead id must not issue document or search requests of its own.
  const baseMissing = baseInfoWindow !== null && baseInfoWindow.status === 'missing';
  const baseReady = baseInfoWindow !== null && baseInfoWindow.status === 'ready';
  const baseLoadFailed = baseInfoWindow !== null && baseInfoWindow.status === 'error';
  const selectedBase = baseInfoWindow?.base
    ?? basesWindow?.items.find((item) => item.id === baseId)
    ?? null;

  // Frozen task bases may sit outside the sidebar window; the map only ever
  // names a base we actually loaded.
  const baseNames = useMemo(() => {
    const names: Record<string, string> = {};
    for (const item of basesState.items) names[item.id] = item.name;
    if (baseInfo.base !== null) names[baseInfo.base.id] = baseInfo.base.name;
    return names;
  }, [basesState, baseInfo]);

  const draftKey = `${baseId ?? ''}|${query}`;
  const searchInput = draft !== null && draft.key === draftKey ? draft.text : query;

  const updateParams = (updates: Record<string, string | null>, replace = false) => {
    setSearchParams((previous) => {
      const next = new URLSearchParams(previous);
      for (const [key, value] of Object.entries(updates)) {
        if (value === null || value === '') next.delete(key);
        else next.set(key, value);
      }
      return next;
    }, { replace });
  };

  const selectBase = (id: string | null) => {
    setDocumentToResume(null);
    updateParams({
      base: id,
      document: null,
      q: null,
      page: null,
      paragraph: null,
      version: null,
      offset: null,
      docs_offset: null,
      search_offset: null,
    }, true);
  };

  const openDocument = (document: KnowledgeDocument) => {
    updateParams({
      base: document.knowledge_base_id,
      document: document.id,
      page: null,
      paragraph: null,
      version: null,
      offset: null,
    });
  };

  const openHit = (hit: SearchHit) => {
    updateParams({
      base: hit.knowledge_base_id,
      document: hit.document_id,
      page: String(hit.page_no),
      // Citations travel 0-based; only the human label adds one.
      paragraph: String(hit.ordinal),
      version: String(hit.version),
      offset: null,
    });
  };

  // ---- Sidebar window --------------------------------------------------

  useEffect(() => {
    const scope = `bases:${basesOffset}`;
    const controller = new AbortController();
    let active = true;
    void (async () => {
      try {
        const rows = await listKnowledgeBases({
          limit: WINDOW_SIZE + 1,
          offset: basesOffset,
          signal: controller.signal,
        });
        if (!active) return;
        setBasesState({
          scope,
          status: 'ready',
          items: rows.slice(0, WINDOW_SIZE),
          hasNext: rows.length > WINDOW_SIZE,
        });
      } catch (error) {
        if (!active || isAbortError(error)) return;
        setBasesState((previous) => ({
          scope,
          status: 'error',
          items: previous.scope === scope ? previous.items : [],
          hasNext: previous.scope === scope ? previous.hasNext : false,
        }));
      }
    })();
    return () => {
      active = false;
      controller.abort();
    };
  }, [basesOffset, basesToken]);

  // ---- The base named by the address -----------------------------------

  useEffect(() => {
    if (baseId === null) return;
    const scope = baseId;
    const controller = new AbortController();
    let active = true;
    void (async () => {
      try {
        const base = await getKnowledgeBase(scope, { signal: controller.signal });
        if (!active) return;
        setBaseInfo({ scope, status: 'ready', base });
      } catch (error) {
        if (!active || isAbortError(error)) return;
        // 401 already redirected globally; the base state stays as it was.
        if (isAuthError(error)) return;
        const missing = error instanceof ApiError && error.status === 404;
        setBaseInfo({ scope, status: missing ? 'missing' : 'error', base: null });
      }
    })();
    return () => {
      active = false;
      controller.abort();
    };
  }, [baseId, baseToken]);

  // ---- First visit: pick the first own base ----------------------------

  // Only the first visit picks a base for the user; after an explicit
  // deselection or a delete the page stays empty instead of jumping elsewhere.
  const autoSelectedRef = useRef(false);

  useEffect(() => {
    if (baseId !== null || documentId !== null) {
      // An explicit base or an open reader ends the "first visit" window.
      autoSelectedRef.current = true;
      return;
    }
    if (autoSelectedRef.current) return;
    if (basesWindow === null || basesWindow.status !== 'ready' || basesWindow.items.length === 0) return;
    autoSelectedRef.current = true;
    setSearchParams((previous) => {
      const next = new URLSearchParams(previous);
      next.set('base', basesWindow.items[0].id);
      return next;
    }, { replace: true });
  }, [baseId, documentId, basesWindow, setSearchParams]);

  // ---- Documents window ------------------------------------------------

  useEffect(() => {
    if (baseId === null || !documentsMode || !baseReady) return;
    const scope = `docs:${baseId}:${docsOffset}`;
    const controller = new AbortController();
    let active = true;
    void (async () => {
      try {
        const rows = await listDocuments(baseId, {
          limit: WINDOW_SIZE + 1,
          offset: docsOffset,
          signal: controller.signal,
        });
        if (!active) return;
        setDocumentsState({
          scope,
          status: 'ready',
          items: rows.slice(0, WINDOW_SIZE),
          hasNext: rows.length > WINDOW_SIZE,
        });
        setDocumentsError(null);
      } catch (error) {
        if (!active || isAbortError(error)) return;
        setDocumentsError(scope);
        const keepRows = keepsVisibleRows(error);
        setDocumentsState((previous) => {
          if (previous.scope !== scope) {
            // Another scope's rows must never be shown under this address.
            return { scope, status: 'error', items: [], hasNext: false };
          }
          // Same scope: a transient failure keeps the visible rows and lets the
          // poll re-arm above them; an unusable window drops them and stops.
          return keepRows
            ? { ...previous, status: 'ready' }
            : { scope, status: 'error', items: [], hasNext: false };
        });
      }
    })();
    return () => {
      active = false;
      controller.abort();
    };
  }, [baseId, baseReady, documentsMode, docsOffset, documentsToken]);

  // ---- Search window ---------------------------------------------------

  useEffect(() => {
    if (baseId === null || !searchMode || !baseReady) return;
    const scope = `search:${baseId}:${searchQuery}:${searchOffset}`;
    const controller = new AbortController();
    let active = true;
    void (async () => {
      try {
        const page = await searchKnowledgeBase(baseId, searchQuery, {
          limit: SEARCH_PAGE_SIZE,
          offset: searchOffset,
          signal: controller.signal,
        });
        if (!active) return;
        setSearchState({ scope, status: 'ready', hits: page.hits, nextOffset: page.next_offset });
        setSearchError(null);
      } catch (error) {
        if (!active || isAbortError(error)) return;
        setSearchError(scope);
        const keepHits = keepsVisibleRows(error);
        setSearchState((previous) => {
          if (previous.scope !== scope) {
            return { scope, status: 'error', hits: [], nextOffset: null };
          }
          return keepHits
            ? { ...previous, status: 'ready' }
            : { scope, status: 'error', hits: [], nextOffset: null };
        });
      }
    })();
    return () => {
      active = false;
      controller.abort();
    };
  }, [baseId, baseReady, searchMode, searchQuery, searchOffset]);

  // ---- Recursive poll of a non-terminal window -------------------------

  useEffect(() => {
    if (!documentsMode) return;
    const current = documentsState.scope === docsScope ? documentsState : null;
    // A loaded window with an error banner still polls: the rows are real and
    // the loop is what lets a stuck import finish without a manual refresh.
    if (current === null || current.status !== 'ready') return;
    if (!current.items.some((item) => POLLING_STATES.has(item.state))) return;
    // One timer per window: it is rescheduled only after the previous request
    // settled, so a slow server can never stack overlapping requests. Upload
    // byte progress re-renders the page but leaves documentsState untouched,
    // so it cannot restart this timer.
    const timer = window.setTimeout(() => setDocumentsToken((token) => token + 1), POLL_INTERVAL_MS);
    return () => window.clearTimeout(timer);
  }, [documentsMode, documentsState, docsScope]);

  // ---- Library actions -------------------------------------------------

  const submitName = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (nameDialog === null || nameSaving) return;
    const name = nameDialog.value.trim();
    if (name === '' || name.length > MAX_NAME_LENGTH) return;
    // The target and the navigation it was opened in are frozen at submit time:
    // a rename started on A must never be applied to whatever base is current
    // when the response lands.
    const { mode, baseId: targetBaseId, key: openedKey } = nameDialog;
    // A late response may only close the dialog it was submitted from: the user
    // may have opened another one while this request was in flight.
    const closeDialog = () => setNameDialog((current) => (
      current !== null && current.key === openedKey && current.mode === mode
        && current.baseId === targetBaseId
        ? null
        : current
    ));
    setNameSaving(true);
    try {
      if (mode === 'create') {
        const created = await createKnowledgeBase(name);
        closeDialog();
        // Only a submission that is still the current navigation may select the
        // new base; mounting the same component elsewhere must not move it.
        if (inCurrentScope(openedKey)) selectBase(created.id);
        setBasesToken((token) => token + 1);
      } else {
        if (targetBaseId === null) return;
        await renameKnowledgeBase(targetBaseId, name);
        closeDialog();
        // The single-window detail state is not written here: if the address
        // moved to another base, writing it would hide that base. The current
        // address is the one renamed -- regardless of how many navigations the
        // slow PATCH outlived (A -> B -> A) -- so its detail may be invalidated.
        if (mountedRef.current && latestBaseIdRef.current === targetBaseId) {
          setBaseInfo(EMPTY_BASE_INFO);
          setBaseToken((token) => token + 1);
        }
        setBasesToken((token) => token + 1);
      }
    } catch (error) {
      // The dialog closes either way; the toast carries the failure, so the
      // list is never rewired as if the write had succeeded.
      if (!isAuthError(error) && inCurrentScope(openedKey)) {
        showToast(t(
          mode === 'create' ? 'knowledge.error.createFailed' : 'knowledge.error.renameFailed',
          'The request failed.',
        ));
      }
      closeDialog();
    } finally {
      setNameSaving(false);
    }
  };

  // ConfirmModal does not catch: swallowing here keeps a failed delete from
  // becoming an unhandled rejection and from pretending the row is gone.
  const confirmDeleteBase = async () => {
    const target = baseToDelete?.base ?? null;
    if (target === null) return;
    // The navigation that opened the dialog, not whatever is current later.
    const openedKey = baseToDelete?.key ?? latestKeyRef.current;
    try {
      await deleteKnowledgeBase(target.id);
    } catch (error) {
      if (!isAuthError(error) && inCurrentScope(openedKey)) {
        showToast(t('knowledge.error.deleteBaseFailed', 'Failed to delete the knowledge base.'));
      }
      return;
    }
    // Settled local rows of this base go; active ones are the server's to end.
    // The hook refuses active rows, so a deleted base can never fake-stop one.
    for (const task of uploads.tasks) {
      if (task.baseId === target.id) uploads.remove(task.id);
    }
    // An unmounted page has nothing to refresh and must not navigate.
    if (!mountedRef.current) return;
    // The deleted base can still be listed in whatever sidebar window the user
    // is looking at, so the list always invalidates; only the selection and the
    // address are restricted to the base that was actually deleted.
    if (inCurrentScope(openedKey) && latestBaseIdRef.current === target.id) selectBase(null);
    setBasesToken((token) => token + 1);
  };

  const confirmDeleteDocument = async () => {
    const target = documentToDelete?.document ?? null;
    if (target === null) return;
    const openedKey = documentToDelete?.key ?? latestKeyRef.current;
    try {
      await deleteDocument(target.id);
    } catch (error) {
      if (!isAuthError(error) && inCurrentScope(openedKey)) {
        showToast(t('knowledge.error.deleteDocumentFailed', 'Failed to delete the document.'));
      }
      return;
    }
    // The frozen task list is global, so the local rows of this document go
    // regardless of where the page has navigated in the meantime.
    for (const task of uploads.tasks) {
      if (task.document?.id === target.id) uploads.remove(task.id);
    }
    // A hint for a document the user is no longer looking at is dropped, and a
    // late dialog callback must not clear a hint for another document.
    setDocumentToResume((current) => (current?.id === target.id ? null : current));
    if (inCurrentScope(openedKey)) setDocumentsToken((token) => token + 1);
  };

  const handleRetryDocument = async (document: KnowledgeDocument) => {
    const openedKey = latestKeyRef.current;
    const openedBaseId = latestBaseIdRef.current;
    // A hint belongs to the document window it was prepared in; a late response
    // after navigating elsewhere must not surface it in the new scope.
    const hintScope = () => inCurrentScope(openedKey)
      && latestBaseIdRef.current === openedBaseId
      && latestDocumentsModeRef.current;
    setRetryingId(document.id);
    try {
      const updated = await retryDocument(document.id);
      // A prepared retry may still need the bytes: the user picks the source
      // file themselves, in their own gesture, and it reuses the same id.
      setDocumentToResume((current) => {
        if (updated.state !== 'pending_upload') {
          return current?.id === document.id ? null : current;
        }
        return hintScope() ? updated : current;
      });
    } catch (error) {
      if (!isAuthError(error) && inCurrentScope(openedKey)) {
        const busy = error instanceof ApiError
          && (error.status === 503 || error.code === 'document.upload_busy');
        showToast(t(busy ? 'knowledge.error.retryBusy' : 'knowledge.error.retryFailed', 'The retry failed.'));
      }
    } finally {
      // Either way the row must show the real server state, so refresh it; a
      // late response must not restart loading in a scope that has moved on.
      if (inCurrentScope(openedKey)) setDocumentsToken((token) => token + 1);
      setRetryingId((current) => (current === document.id ? null : current));
    }
  };

  const resumeWithFile = (document: KnowledgeDocument, file: File) => {
    uploads.resumeDocument(document, file);
    setDocumentToResume(null);
    setDocumentsToken((token) => token + 1);
  };

  // ---- Uploads ---------------------------------------------------------

  const addFiles = (files: File[]) => {
    if (baseId === null || files.length === 0) return;
    uploads.addFiles(baseId, files);
  };

  const handleDrop = (event: DragEvent<HTMLDivElement>) => {
    event.preventDefault();
    addFiles(Array.from(event.dataTransfer?.files ?? []));
  };

  const handleFilePick = (event: ChangeEvent<HTMLInputElement>) => {
    const input = event.currentTarget;
    const files = Array.from(input.files ?? []);
    input.value = '';
    addFiles(files);
  };

  const submitSearch = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const submitted = searchInput.trim().slice(0, 500);
    // The handler runs in the navigation it was submitted from; a deferred
    // commit would write this address after the user moved on.
    updateParams({ q: submitted === '' ? null : submitted, search_offset: null }, true);
  };

  // ---- Render ----------------------------------------------------------

  const docStateLabel = (document: KnowledgeDocument) =>
    t(`knowledge.reader.state.${document.state}`, document.state);
  const docErrorLabel = (document: KnowledgeDocument) =>
    document.error_code === null
      ? ''
      : t(`knowledge.reader.error.${document.error_code}`, document.error_code);
  const mimeLabel = (document: KnowledgeDocument) => {
    if (document.mime_type === 'application/pdf') return t('knowledge.library.mimePdf', 'PDF');
    if (document.mime_type === 'text/plain') return t('knowledge.library.mimeText', 'Text');
    return '';
  };

  const baseList = basesWindow === null ? (
    <LoadingIndicator isLoading />
  ) : basesWindow.status === 'error' ? (
    <p className="knowledge-alert is-error" role="alert">{t('knowledge.library.listFailed', 'Failed to load the knowledge bases.')}</p>
  ) : basesWindow.items.length === 0 ? (
    <EmptyState message={t('knowledge.library.empty', 'No knowledge base yet.')} />
  ) : (
    <ul className="knowledge-base-list-items">
      {basesWindow.items.map((base) => (
        <li key={base.id}>
          <button
            type="button"
            className={`knowledge-base-item${base.id === baseId ? ' is-active' : ''}`}
            aria-current={base.id === baseId ? 'true' : undefined}
            onClick={() => selectBase(base.id)}
          >
            {base.name}
          </button>
        </li>
      ))}
    </ul>
  );

  const searchForm = (
    <form className="knowledge-search" role="search" onSubmit={submitSearch}>
      <label className="knowledge-search-label" htmlFor={searchInputId}>
        {t('knowledge.library.searchLabel', 'Full-text search')}
      </label>
      <input
        id={searchInputId}
        className="knowledge-input"
        type="search"
        maxLength={500}
        value={searchInput}
        placeholder={t('knowledge.library.searchPlaceholder', 'Search this knowledge base')}
        onChange={(event) => setDraft({ key: draftKey, text: event.target.value })}
      />
      <button type="submit" className="btn">{t('knowledge.library.searchSubmit', 'Search')}</button>
      {query !== '' && (
        <button type="button" className="btn ghost" onClick={() => updateParams({ q: null, search_offset: null })}>
          {t('knowledge.library.searchClear', 'Clear search')}
        </button>
      )}
    </form>
  );

  const dropZone = (
    <div className="knowledge-drop" onDragOver={(event) => event.preventDefault()} onDrop={handleDrop}>
      <p className="knowledge-drop-text">{t('knowledge.upload.drop', 'Drop PDF or TXT files here.')}</p>
      <label className="btn knowledge-file-label">
        {t('knowledge.upload.select', 'Choose files')}
        <input
          className="knowledge-file-input"
          type="file"
          multiple
          accept=".pdf,.txt"
          onChange={handleFilePick}
        />
      </label>
      <p className="knowledge-upload-hint">{t('knowledge.upload.hint', 'PDF and plain text, up to 64 MiB each.')}</p>
    </div>
  );

  const documentsSection = (
    <section className="knowledge-workspace" aria-label={t('knowledge.library.documents', 'Documents')}>
      <div className="knowledge-toolbar">
        <button type="button" className="btn ghost" onClick={() => setDocumentsToken((token) => token + 1)}>
          {t('actions.refresh', 'Refresh')}
        </button>
        {documentsWindow !== null && documentsWindow.items.length > 0 && (
          <span className="knowledge-range">
            {fill(t('knowledge.library.windowRange', 'Items {from}-{to}'), {
              from: docsOffset + 1,
              to: docsOffset + documentsWindow.items.length,
            })}
          </span>
        )}
        <div className="knowledge-window-nav">
          <button
            type="button"
            className="btn ghost"
            disabled={docsOffset <= 0}
            onClick={() => updateParams({ docs_offset: String(Math.max(0, docsOffset - WINDOW_SIZE)) })}
          >
            {t('knowledge.library.prevWindow', 'Previous')}
          </button>
          <button
            type="button"
            className="btn ghost"
            disabled={documentsWindow === null || !documentsWindow.hasNext}
            onClick={() => updateParams({ docs_offset: String(docsOffset + WINDOW_SIZE) })}
          >
            {t('knowledge.library.nextWindow', 'Next')}
          </button>
        </div>
      </div>
      {documentsError === docsScope && (
        <p className="knowledge-alert is-error" role="alert">
          {t('knowledge.library.documentsFailed', 'Failed to load the documents.')}
        </p>
      )}
      {documentsWindow === null ? (
        <LoadingIndicator isLoading />
      ) : documentsWindow.items.length === 0 ? (
        documentsError === docsScope
          ? null
          : <EmptyState message={t('knowledge.library.documentsEmpty', 'This knowledge base has no documents.')} />
      ) : (
        <ul className="knowledge-doc-list">
          {documentsWindow.items.map((document) => (
            <li className="knowledge-doc" key={document.id}>
              <div className="knowledge-doc-main">
                {document.state === 'ready' ? (
                  <button type="button" className="knowledge-doc-name" onClick={() => openDocument(document)}>
                    {document.filename}
                  </button>
                ) : (
                  <span className="knowledge-doc-name">{document.filename}</span>
                )}
                <p className="knowledge-doc-meta">
                  <span>{mimeLabel(document)}</span>
                  {document.size_bytes !== null && <span>{formatBytes(document.size_bytes)}</span>}
                  <span>{docStateLabel(document)}</span>
                  {document.error_code !== null && <span>{docErrorLabel(document)}</span>}
                </p>
                {(document.state === 'pending_upload' || documentToResume?.id === document.id) && (
                  <p className="knowledge-hint" role="status">
                    {fill(t('knowledge.upload.pendingFile', 'Select {name} again.'), { name: document.filename })}
                  </p>
                )}
              </div>
              <div className="knowledge-doc-actions">
                {document.state === 'ready' && (
                  <button type="button" className="btn ghost" onClick={() => openDocument(document)}>
                    {t('knowledge.library.open', 'Open')}
                  </button>
                )}
                {document.state === 'failed' && (
                  <button
                    type="button"
                    className="btn ghost"
                    disabled={retryingId === document.id}
                    onClick={() => { void handleRetryDocument(document); }}
                  >
                    {t('knowledge.upload.retry', 'Retry')}
                  </button>
                )}
                {(document.state === 'pending_upload' || documentToResume?.id === document.id) && (
                  <label className="btn knowledge-file-label">
                    {t('knowledge.upload.chooseFile', 'Choose source file')}
                    <input
                      className="knowledge-file-input"
                      type="file"
                      accept=".pdf,.txt"
                      onChange={(event) => {
                        const input = event.currentTarget;
                        const file = input.files?.[0];
                        input.value = '';
                        if (file) resumeWithFile(document, file);
                      }}
                    />
                  </label>
                )}
                <button type="button" className="btn ghost" onClick={() => setDocumentToDelete({
                  document,
                  key: latestKeyRef.current,
                })}>
                  {t('common.delete', 'Delete')}
                </button>
              </div>
            </li>
          ))}
        </ul>
      )}
    </section>
  );

  const searchSection = (
    <section className="knowledge-workspace" aria-label={t('knowledge.library.searchLabel', 'Full-text search')}>
      <div className="knowledge-toolbar">
        <div className="knowledge-window-nav">
          <button
            type="button"
            className="btn ghost"
            disabled={searchOffset <= 0}
            onClick={() => updateParams({ search_offset: String(Math.max(0, searchOffset - SEARCH_PAGE_SIZE)) })}
          >
            {t('knowledge.library.searchPrev', 'Previous')}
          </button>
          <button
            type="button"
            className="btn ghost"
            disabled={searchWindow === null || searchWindow.nextOffset === null}
            onClick={() => {
              const next = searchWindow?.nextOffset;
              if (next !== null && next !== undefined) updateParams({ search_offset: String(next) });
            }}
          >
            {t('knowledge.library.searchNext', 'Next')}
          </button>
        </div>
      </div>
      {searchError === searchScope && (
        <p className="knowledge-alert is-error" role="alert">{t('knowledge.library.searchFailed', 'The search failed.')}</p>
      )}
      {searchWindow === null ? (
        <LoadingIndicator isLoading />
      ) : searchWindow.hits.length === 0 ? (
        searchError === searchScope
          ? null
          : <EmptyState message={t('knowledge.library.searchEmpty', 'No matching text.')} />
      ) : (
        <ul className="knowledge-hit-list">
          {searchWindow.hits.map((hit) => (
            <li key={`${hit.document_id}:${hit.page_no}:${hit.ordinal}`}>
              <button type="button" className="knowledge-hit" onClick={() => openHit(hit)}>
                <span className="knowledge-hit-meta">
                  {fill(t('knowledge.library.hitMeta', '{filename} · page {page} · paragraph {ordinal}'), {
                    filename: hit.filename,
                    page: hit.page_no,
                    ordinal: hit.ordinal + 1,
                  })}
                </span>
                {/* Plain text node: the excerpt is never HTML. */}
                <span className="knowledge-hit-text">{hit.text}</span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </section>
  );

  let workspace = documentsSection;
  if (documentId !== null) {
    workspace = <DocumentReader documentId={documentId} />;
  } else if (baseId === null) {
    workspace = <EmptyState message={t('knowledge.upload.needBase', 'Create a knowledge base first.')} />;
  } else if (baseLoadFailed) {
    workspace = (
      <div className="knowledge-alert is-error" role="alert">
        <p>{t('knowledge.error.baseLoadFailed', 'Failed to load the knowledge base.')}</p>
        <button type="button" className="btn ghost" onClick={() => setBaseToken((token) => token + 1)}>
          {t('actions.refresh', 'Refresh')}
        </button>
      </div>
    );
  } else if (baseMissing) {
    workspace = <p className="knowledge-alert is-error" role="alert">{t('knowledge.error.baseNotFound', 'This knowledge base does not exist.')}</p>;
  } else if (searchMode) {
    workspace = searchSection;
  }

  return (
    <section id="view-knowledge" className="view is-active knowledge-page">
      <div className="knowledge-layout">
        <aside className={`panel knowledge-sidebar${sidebarOpen ? ' is-open' : ''}`}>
          <button
            type="button"
            className="btn ghost knowledge-sidebar-toggle"
            aria-expanded={sidebarOpen}
            onClick={() => setSidebarOpen((open) => !open)}
          >
            {sidebarOpen
              ? t('knowledge.library.sidebarHide', 'Hide knowledge bases')
              : t('knowledge.library.sidebarShow', 'Show knowledge bases')}
          </button>
          <div className="panel-header knowledge-sidebar-header">
            <div>
              <h2>{t('knowledge.library.title', 'Knowledge')}</h2>
              <p className="muted">{t('knowledge.library.subtitle', 'Manage documents per knowledge base.')}</p>
            </div>
            <button
              type="button"
              className="btn"
              disabled={nameSaving}
              onClick={() => setNameDialog({
                mode: 'create',
                value: '',
                baseId: null,
                key: latestKeyRef.current,
              })}
            >
              {t('knowledge.library.create', 'New base')}
            </button>
          </div>
          {nameDialog !== null && (
            <form className="knowledge-name-form" onSubmit={(event) => { void submitName(event); }}>
              <label className="knowledge-label" htmlFor={nameInputId}>
                {nameDialog.mode === 'create'
                  ? t('knowledge.library.createPrompt', 'New base name')
                  : t('knowledge.library.rename', 'Rename')}
              </label>
              <input
                id={nameInputId}
                className="knowledge-input"
                type="text"
                autoFocus
                maxLength={MAX_NAME_LENGTH}
                placeholder={t('knowledge.library.createPlaceholder', 'Knowledge base name')}
                value={nameDialog.value}
                onChange={(event) => setNameDialog((dialog) => (
                  dialog === null ? dialog : { ...dialog, value: event.target.value }
                ))}
              />
              <div className="knowledge-name-actions">
                <button type="button" className="btn ghost" disabled={nameSaving} onClick={() => setNameDialog(null)}>
                  {t('common.cancel', 'Cancel')}
                </button>
                <button type="submit" className="btn" disabled={nameSaving || nameDialog.value.trim() === ''}>
                  {t('common.save', 'Save')}
                </button>
              </div>
            </form>
          )}
          <div className="knowledge-base-list">{baseList}</div>
          <div className="knowledge-window-nav">
            <button
              type="button"
              className="btn ghost"
              disabled={basesOffset <= 0}
              onClick={() => updateParams({ bases_offset: String(Math.max(0, basesOffset - WINDOW_SIZE)) })}
            >
              {t('knowledge.library.prevWindow', 'Previous')}
            </button>
            <button
              type="button"
              className="btn ghost"
              disabled={basesWindow === null || !basesWindow.hasNext}
              onClick={() => updateParams({ bases_offset: String(basesOffset + WINDOW_SIZE) })}
            >
              {t('knowledge.library.nextWindow', 'Next')}
            </button>
          </div>
        </aside>

        <div className="knowledge-main">
          <header className="knowledge-main-head">
            <div className="knowledge-main-title">
              <h2>{selectedBase?.name ?? t('knowledge.library.title', 'Knowledge')}</h2>
            </div>
            {selectedBase !== null && baseReady && (
              <div className="toolbar knowledge-main-actions">
                <button
                  type="button"
                  className="btn ghost"
                  onClick={() => setNameDialog({
                    mode: 'rename',
                    value: selectedBase.name,
                    baseId: selectedBase.id,
                    key: latestKeyRef.current,
                  })}
                >
                  {t('knowledge.library.rename', 'Rename')}
                </button>
                <button type="button" className="btn ghost" onClick={() => setBaseToDelete({
                  base: selectedBase,
                  key: latestKeyRef.current,
                })}>
                  {t('common.delete', 'Delete')}
                </button>
              </div>
            )}
          </header>

          {documentId === null && baseReady && searchForm}
          {documentsMode && baseReady && dropZone}
          {uploads.tasks.length > 0 && (
            <UploadQueue
              tasks={uploads.tasks}
              paused={uploads.paused}
              baseNames={baseNames}
              onRetry={uploads.retry}
              onRemove={uploads.remove}
              onResume={uploads.resume}
              onOpenDocument={openDocument}
            />
          )}
          {workspace}
        </div>
      </div>

      <ConfirmModal
        isOpen={baseToDelete !== null}
        title={t('knowledge.library.deleteConfirmTitle', 'Delete knowledge base')}
        message={fill(t('knowledge.library.deleteConfirmMessage', 'Delete "{name}"? Server-side files may remain.'), {
          name: baseToDelete?.base.name ?? '',
        })}
        confirmLabel={t('common.delete', 'Delete')}
        onClose={() => setBaseToDelete(null)}
        onConfirm={confirmDeleteBase}
      />
      <ConfirmModal
        isOpen={documentToDelete !== null}
        title={t('knowledge.library.deleteDocumentTitle', 'Delete document')}
        message={fill(t('knowledge.library.deleteDocumentMessage', 'Delete "{name}"? Server-side files may remain.'), {
          name: documentToDelete?.document.filename ?? '',
        })}
        confirmLabel={t('common.delete', 'Delete')}
        onClose={() => setDocumentToDelete(null)}
        onConfirm={confirmDeleteDocument}
      />
    </section>
  );
}
