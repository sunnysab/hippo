import { useEffect, useId, useRef, useState, type FormEvent } from 'react';
import { useSearchParams } from 'react-router-dom';
import { ApiError, isAuthError } from '../../api';
import {
  getDocument,
  getDocumentContent,
  type DocumentChunk,
  type KnowledgeDocument,
} from '../../api/knowledge';
import { useI18n } from '../../i18n';
import { LoadingIndicator } from '../../components/LoadingIndicator';
import { EmptyState } from '../../components/EmptyState';
import '../../styles/knowledge-reader.css';

/** Chunks fetched per request; the whole document is never appended at once. */
const WINDOW_SIZE = 100;

/** ``null``/blank means "absent"; a present value must be a valid integer. */
type ParamResult = { ok: true; value: number | undefined } | { ok: false };

const parseParam = (raw: string | null, min: number): ParamResult => {
  if (raw === null || raw.trim() === '') return { ok: true, value: undefined };
  const trimmed = raw.trim();
  const value = Number(trimmed);
  if (!/^\d+$/.test(trimmed) || !Number.isSafeInteger(value) || value < min) return { ok: false };
  return { ok: true, value };
};

interface LoadedContent {
  document: KnowledgeDocument;
  chunks: DocumentChunk[];
  nextOffset: number | null;
}

/** Every async result is tagged with the request that produced it, so a late
 * success or failure can never be shown for another document or window. */
type ReaderState =
  | { key: string; kind: 'loading' }
  | { key: string; kind: 'content'; data: LoadedContent }
  | { key: string; kind: 'not-ready'; document: KnowledgeDocument | null }
  | { key: string; kind: 'error'; reason: 'not-found' | 'page' | 'generic' };

/** Only the server's page-level code means a missing page; a document 404 is a
 * missing document no matter which page was requested. */
const errorReason = (error: unknown): 'not-found' | 'page' | 'generic' => {
  if (!(error instanceof ApiError) || error.status !== 404) return 'generic';
  if (error.code === 'document.page_not_found') return 'page';
  if (error.code === 'document.not_found') return 'not-found';
  // A 404 with no known code is reported as a load failure: neither the page
  // nor the document is claimed to be gone.
  return 'generic';
};

const isAbortError = (error: unknown) => (error as Error | null)?.name === 'AbortError';

/** The i18n dictionary keeps ``{name}`` placeholders; fill them here. */
const fill = (template: string, values: Record<string, string | number>) =>
  Object.entries(values).reduce(
    (text, [key, value]) => text.replace(`{${key}}`, String(value)),
    template,
  );

export function DocumentReader({ documentId }: { documentId: string }) {
  const { t } = useI18n();
  const [searchParams, setSearchParams] = useSearchParams();
  const gotoInputId = useId();

  const pageParam = parseParam(searchParams.get('page'), 1);
  const offsetParam = parseParam(searchParams.get('offset'), 0);
  const paragraphParam = parseParam(searchParams.get('paragraph'), 0);
  const versionParam = parseParam(searchParams.get('version'), 1);
  const paramsValid = pageParam.ok && offsetParam.ok && paragraphParam.ok && versionParam.ok;

  const page = pageParam.ok && pageParam.value !== undefined ? pageParam.value : 1;
  const explicitOffset = offsetParam.ok ? offsetParam.value : undefined;
  const paragraph = paragraphParam.ok ? paragraphParam.value : undefined;
  const version = versionParam.ok ? versionParam.value : undefined;

  // shortcut: the initial window is derived from a contiguous per-page ordinal
  // (floor(ordinal / 100) * 100), which the current parser guarantees. A
  // fragmented ordinal would need scanning batches, so a citation outside the
  // window is reported as missing instead of being chased.
  const paragraphWindow = paragraph === undefined ? 0 : Math.floor(paragraph / WINDOW_SIZE) * WINDOW_SIZE;
  const effectiveOffset = explicitOffset ?? paragraphWindow;

  const [reloadToken, setReloadToken] = useState(0);
  const [pageDraft, setPageDraft] = useState<{ page: number; text: string } | null>(null);
  const [inputError, setInputError] = useState<{ page: number; message: string } | null>(null);
  const requestRef = useRef(0);
  const scrollRef = useRef<HTMLDivElement>(null);

  const requestKey = `${documentId}|${page}|${effectiveOffset}|${reloadToken}`;
  const [state, setState] = useState<ReaderState>({ key: requestKey, kind: 'loading' });

  // Input and validation are keyed by page instead of reset by an effect, so
  // navigating never shows a draft or an error from the previous page.
  const pageInput = pageDraft !== null && pageDraft.page === page ? pageDraft.text : String(page);
  const pageError = inputError !== null && inputError.page === page ? inputError.message : '';

  useEffect(() => {
    if (!paramsValid) return;
    const key = `${documentId}|${page}|${effectiveOffset}|${reloadToken}`;
    const requestId = requestRef.current + 1;
    requestRef.current = requestId;
    const controller = new AbortController();
    let active = true;

    const isStale = () => !active || requestRef.current !== requestId;

    const load = async () => {
      try {
        const content = await getDocumentContent(documentId, {
          page_no: page,
          limit: WINDOW_SIZE,
          offset: effectiveOffset,
          signal: controller.signal,
        });
        if (isStale()) return;
        const pageCount = content.document.page_count;
        if (pageCount !== null && page > pageCount) {
          setState({ key, kind: 'error', reason: 'page' });
          return;
        }
        setState({
          key,
          kind: 'content',
          data: {
            document: content.document,
            chunks: content.chunks,
            nextOffset: content.next_offset,
          },
        });
      } catch (error) {
        if (isStale() || isAbortError(error)) return;
        // 401 already triggered the global redirect; a local message would
        // only compete with the login screen.
        if (isAuthError(error)) return;
        if (error instanceof ApiError && error.status === 409) {
          try {
            const document = await getDocument(documentId, { signal: controller.signal });
            if (isStale()) return;
            setState({ key, kind: 'not-ready', document });
          } catch (documentError) {
            if (isStale() || isAbortError(documentError) || isAuthError(documentError)) return;
            setState({ key, kind: 'error', reason: errorReason(documentError) });
          }
          return;
        }
        setState({ key, kind: 'error', reason: errorReason(error) });
      }
    };

    void load();
    return () => {
      active = false;
      controller.abort();
    };
  }, [documentId, page, effectiveOffset, paramsValid, reloadToken]);

  // A result from another document, page or window is not shown at all, so the
  // previous text never flashes under the new address.
  const visible: ReaderState = state.key === requestKey ? state : { key: requestKey, kind: 'loading' };
  const data = visible.kind === 'content' ? visible.data : null;
  const headerDocument = data !== null
    ? data.document
    : visible.kind === 'not-ready' ? visible.document : null;

  const versionMatches = version === undefined || (data !== null && version === data.document.version);
  const focusOrdinal = data !== null && paragraph !== undefined && versionMatches ? paragraph : null;
  const focusCandidates = data === null || focusOrdinal === null
    ? []
    : data.chunks.filter((candidate) => candidate.page_no === page && candidate.ordinal === focusOrdinal);
  const focusedChunk = focusCandidates.length === 1 ? focusCandidates[0] : null;

  const referenceNotice = data === null
    ? null
    : version !== undefined && version !== data.document.version
      ? 'version'
      : paragraph !== undefined && focusedChunk === null ? 'missing' : null;

  useEffect(() => {
    if (focusedChunk === null) return;
    const node = scrollRef.current?.querySelector('.knowledge-reader-chunk.is-focused');
    if (node && typeof node.scrollIntoView === 'function') node.scrollIntoView({ block: 'center' });
  }, [focusedChunk]);

  const setWindow = (nextPage: number, nextOffset: number | undefined) => {
    const next = new URLSearchParams(searchParams);
    if (nextPage <= 1) next.delete('page');
    else next.set('page', String(nextPage));
    if (nextOffset === undefined || nextOffset <= 0) next.delete('offset');
    else next.set('offset', String(nextOffset));
    // A manual window change drops the citation; it can never point at a page
    // that is not there.
    next.delete('paragraph');
    // A draft or a validation error belongs to the window it was typed in, so
    // returning to this page later starts from the real page number.
    setPageDraft(null);
    setInputError(null);
    setSearchParams(next);
  };

  const nextOffset = data?.nextOffset ?? null;
  // A next window that does not advance would loop; next_offset is trusted only
  // when it moves forward.
  const canGoNextBatch = data !== null && nextOffset !== null && nextOffset > effectiveOffset;

  // shortcut: the previous batch steps back one window, which assumes the
  // server's offsets advance by the 100-chunk limit; a non-uniform step would
  // need a per-page offset trail.
  const goPrevBatch = () => {
    const previous = Math.max(0, effectiveOffset - WINDOW_SIZE);
    if (previous === effectiveOffset) return;
    setWindow(page, previous);
  };

  const openCurrentVersion = () => {
    const next = new URLSearchParams(searchParams);
    next.delete('version');
    next.delete('paragraph');
    setSearchParams(next);
  };

  const backToLibrary = () => {
    const base = searchParams.get('base') || data?.document.knowledge_base_id || '';
    const next = new URLSearchParams();
    if (base) next.set('base', base);
    setSearchParams(next);
  };

  const submitPage = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const nextPage = Number(pageInput.trim());
    if (!Number.isInteger(nextPage) || nextPage < 1) {
      setInputError({ page, message: t('knowledge.reader.invalidPageInput', 'Enter a valid page number.') });
      return;
    }
    setInputError(null);
    if (nextPage === page) return;
    setWindow(nextPage, undefined);
  };

  const pageCount = data?.document.page_count ?? null;
  const documentNotFound = visible.kind === 'error' && visible.reason === 'not-found';
  const showFirstPage = visible.kind === 'error' && !documentNotFound
    && (visible.reason === 'page' || page > 1);
  const showRefresh = visible.kind === 'error' && visible.reason === 'generic';
  // A 409 can race a document that is already ready again; the state stays the
  // real one and the not-ready copy is only shown when it is still true.
  const notReadyState = visible.kind === 'not-ready' ? visible.document?.state ?? null : null;
  const notReadyCopy = notReadyState !== 'ready';

  const notReadyFailed = visible.kind === 'not-ready' && (
    visible.document === null ||
    visible.document.state === 'failed' ||
    visible.document.state === 'unsupported' ||
    visible.document.state === 'deleted'
  );

  return (
    <section className="knowledge-reader panel" aria-label={t('knowledge.reader.title', 'Document reader')}>
      <header className="knowledge-reader-head">
        <div className="knowledge-reader-heading">
          <h2 className="knowledge-reader-title">
            {headerDocument?.filename ?? t('knowledge.reader.untitled', 'Document')}
          </h2>
          {data !== null && (
            <p className="knowledge-reader-meta">
              {pageCount !== null
                ? fill(t('knowledge.reader.pagePosition', 'Page {page} of {pages}'), {
                  page,
                  pages: pageCount,
                })
                : fill(t('knowledge.reader.pageOnly', 'Page {page}'), { page })}
              {` · ${fill(t('knowledge.reader.windowAt', 'Window offset {offset}'), { offset: effectiveOffset })}`}
            </p>
          )}
        </div>
        <div className="knowledge-reader-toolbar">
          <button
            type="button"
            className="btn ghost knowledge-reader-action"
            onClick={backToLibrary}
          >
            {t('knowledge.reader.back', 'Back to library')}
          </button>
          <button
            type="button"
            className="btn ghost knowledge-reader-action"
            onClick={() => setWindow(page - 1, undefined)}
            disabled={data === null || page <= 1}
          >
            {t('knowledge.reader.prevPage', 'Previous page')}
          </button>
          <button
            type="button"
            className="btn ghost knowledge-reader-action"
            onClick={() => setWindow(page + 1, undefined)}
            disabled={data === null || (pageCount !== null && page >= pageCount)}
          >
            {t('knowledge.reader.nextPage', 'Next page')}
          </button>
          <form
            className="knowledge-reader-goto"
            aria-label={t('knowledge.reader.pageForm', 'Page navigation')}
            onSubmit={submitPage}
          >
            <label className="knowledge-reader-goto-label" htmlFor={gotoInputId}>
              {t('knowledge.reader.pageLabel', 'Page number')}
            </label>
            <input
              id={gotoInputId}
              className="knowledge-reader-goto-input"
              inputMode="numeric"
              value={pageInput}
              onChange={(event) => setPageDraft({ page, text: event.target.value })}
              disabled={data === null}
            />
            <button type="submit" className="btn ghost knowledge-reader-action" disabled={data === null}>
              {t('knowledge.reader.go', 'Go')}
            </button>
          </form>
          <button
            type="button"
            className="btn ghost knowledge-reader-action"
            onClick={goPrevBatch}
            disabled={data === null || effectiveOffset <= 0}
          >
            {t('knowledge.reader.prevBatch', 'Previous batch')}
          </button>
          <button
            type="button"
            className="btn ghost knowledge-reader-action"
            onClick={() => data !== null && nextOffset !== null && setWindow(page, nextOffset)}
            disabled={!canGoNextBatch}
          >
            {t('knowledge.reader.nextBatch', 'Next batch')}
          </button>
        </div>
      </header>

      {pageError !== '' && (
        <p className="knowledge-reader-alert is-error" role="alert">{pageError}</p>
      )}

      {!paramsValid ? (
        <p className="knowledge-reader-alert is-error" role="alert">
          {t('knowledge.reader.invalidParams', 'The address contains invalid parameters.')}
        </p>
      ) : visible.kind === 'loading' ? (
        <LoadingIndicator isLoading />
      ) : visible.kind === 'error' ? (
        <div className="knowledge-reader-alert is-error" role="alert">
          <p className="knowledge-reader-alert-text">
            {visible.reason === 'not-found'
              ? t('knowledge.reader.notFound', 'This document does not exist or was deleted.')
              : visible.reason === 'page'
                ? t('knowledge.reader.pageNotFound', 'This page does not exist.')
                : t('knowledge.reader.loadFailed', 'Failed to load the document text.')}
          </p>
          <div className="knowledge-reader-alert-actions">
            {showFirstPage && (
              <button
                type="button"
                className="btn ghost knowledge-reader-action"
                onClick={() => setWindow(1, undefined)}
              >
                {t('knowledge.reader.firstPage', 'Back to first page')}
              </button>
            )}
            {showRefresh && (
              <button
                type="button"
                className="btn ghost knowledge-reader-action"
                onClick={() => setReloadToken((token) => token + 1)}
              >
                {t('actions.refresh', 'Refresh')}
              </button>
            )}
            <button type="button" className="btn ghost knowledge-reader-action" onClick={backToLibrary}>
              {t('knowledge.reader.back', 'Back to library')}
            </button>
          </div>
        </div>
      ) : visible.kind === 'not-ready' ? (
        <div
          className={`knowledge-reader-alert${notReadyFailed ? ' is-error' : ''}`}
          role={notReadyFailed ? 'alert' : 'status'}
        >
          <p className="knowledge-reader-alert-text">
            {notReadyCopy
              ? t('knowledge.reader.notReadyTitle', 'The document text is not ready yet.')
              : t('knowledge.reader.loadFailed', 'Failed to load the document text.')}
          </p>
          <p className="knowledge-reader-meta">
            {visible.document !== null
              ? t(`knowledge.reader.state.${visible.document.state}`, visible.document.state)
              : t('knowledge.reader.stateUnknown', 'Unknown state')}
          </p>
          {visible.document?.error_code != null && (
            <p className="knowledge-reader-alert-text">
              {t(`knowledge.reader.error.${visible.document.error_code}`, visible.document.error_code)}
            </p>
          )}
          <div className="knowledge-reader-alert-actions">
            <button
              type="button"
              className="btn ghost knowledge-reader-action"
              onClick={() => setReloadToken((token) => token + 1)}
            >
              {t('actions.refresh', 'Refresh')}
            </button>
            <button type="button" className="btn ghost knowledge-reader-action" onClick={backToLibrary}>
              {t('knowledge.reader.back', 'Back to library')}
            </button>
          </div>
        </div>
      ) : (
        <>
          {referenceNotice === 'version' && (
            <div className="knowledge-reader-warning" role="status">
              <span>{t('knowledge.reader.versionChanged', 'The document was updated; the cited position may have moved.')}</span>
              <button type="button" className="btn ghost knowledge-reader-action" onClick={openCurrentVersion}>
                {t('knowledge.reader.openCurrent', 'Open current version')}
              </button>
            </div>
          )}
          {referenceNotice === 'missing' && (
            <p className="knowledge-reader-warning" role="status">
              {t('knowledge.reader.paragraphNotFound', 'The cited paragraph is not in this page window.')}
            </p>
          )}
          {data !== null && (data.chunks.length === 0 ? (
            <EmptyState message={t('knowledge.reader.emptyPage', 'This page has no extractable text.')} />
          ) : (
            <div className="knowledge-reader-scroll" ref={scrollRef}>
              {data.chunks.map((chunk) => (
                <p
                  key={`${chunk.page_no}-${chunk.ordinal}`}
                  className={`knowledge-reader-chunk${chunk === focusedChunk ? ' is-focused' : ''}`}
                >
                  {chunk.text}
                </p>
              ))}
            </div>
          ))}
        </>
      )}
    </section>
  );
}
