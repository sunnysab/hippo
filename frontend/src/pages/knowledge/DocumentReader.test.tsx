import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter, useLocation } from 'react-router-dom';
import zhCN from '../../i18n/zh-CN.json';
import { I18nProvider } from '../../i18n';
import { ApiError } from '../../api';
import {
  getDocument,
  getDocumentContent,
  type DocumentChunk,
  type DocumentContent,
  type KnowledgeDocument,
} from '../../api/knowledge';
import { DocumentReader } from './DocumentReader';

vi.mock('../../api/knowledge', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../api/knowledge')>();
  return {
    ...actual,
    getDocument: vi.fn(),
    getDocumentContent: vi.fn(),
  };
});

const mockedContent = vi.mocked(getDocumentContent);
const mockedDocument = vi.mocked(getDocument);

// React Router 6.30 warns about the React 19 future flags. The test opts into
// the v7 behaviour it already uses; the app's runtime Router is untouched.
const ROUTER_FUTURE = {
  v7_startTransition: true,
  v7_relativeSplatPath: true,
};

const documentFixture = (overrides: Partial<KnowledgeDocument> = {}): KnowledgeDocument => ({
  id: 'doc-1',
  knowledge_base_id: 'kb-1',
  // A CJK filename, escaped so the test source stays ASCII like the app code.
  filename: '\u8bba\u6587.pdf',
  mime_type: 'application/pdf',
  size_bytes: 1024,
  state: 'ready',
  attempts: 1,
  error_code: null,
  error_message: null,
  page_count: 3,
  version: 4,
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
  ...overrides,
});

const chunkFixture = (page_no: number, ordinal: number, text: string): DocumentChunk => ({
  page_no,
  ordinal,
  text,
  chunk_type: 'paragraph',
  heading_level: null,
});

const contentFixture = (overrides: Partial<DocumentContent> = {}): DocumentContent => ({
  document: documentFixture(),
  chunks: [chunkFixture(1, 0, 'first paragraph')],
  next_offset: null,
  ...overrides,
});

/** The cited paragraph as the reader would render it: one CJK line, escaped
 * so the source stays ASCII and the length assertion keeps its meaning. */
const CITED_PARAGRAPH_TEXT = '\u8fd9\u662f\u4e00\u6bb5\u88ab\u5f15\u7528\u7684\u6b63\u6587\u3002';

// The tests assert on user-visible text. The dictionary stays the single source
// of those strings: this mirrors the provider's lookup without a Hook, since a
// Hook cannot feed module-level assertions.
const zhDictionary = zhCN as Record<string, string>;
const t = (key: string, fallback?: string): string => zhDictionary[key] ?? fallback ?? key;

const deferred = <T,>() => {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
};

const LocationProbe = () => <span data-testid="location">{useLocation().search}</span>;

// The router mounts once per test; switching ``documentId`` is a prop change,
// exactly like the future single /knowledge page re-rendering the reader.
const renderReader = (entry: string, documentId: string) => {
  const element = (id: string) => (
    <I18nProvider>
      <MemoryRouter initialEntries={[entry]} future={ROUTER_FUTURE}>
        <DocumentReader documentId={id} />
        <LocationProbe />
      </MemoryRouter>
    </I18nProvider>
  );
  const result = render(element(documentId));
  return { ...result, switchTo: (id: string) => result.rerender(element(id)) };
};

const location = () => screen.getByTestId('location').textContent;

/** Matches what the reader renders for "page X of Y" through its own keys. */
const pagePosition = (page: number, pages: number) =>
  t('knowledge.reader.pagePosition').replace('{page}', String(page)).replace('{pages}', String(pages));

/** The header line carries the position plus the window offset, so assert on
 * the element's text instead of an exact whole-node match. */
const pageMeta = (container: HTMLElement) => container.querySelector('.knowledge-reader-meta')?.textContent ?? '';

beforeEach(() => {
  mockedContent.mockReset();
  mockedDocument.mockReset();
  // jsdom has no scrolling; stubbing here keeps the component free of runtime
  // browser shims.
  HTMLElement.prototype.scrollIntoView = vi.fn();
});

afterEach(() => {
  vi.clearAllMocks();
});

describe('DocumentReader', () => {
  it('requests the first 100-chunk window by default', async () => {
    mockedContent.mockResolvedValue(contentFixture());

    const { container } = renderReader('/?base=kb-1&document=doc-1', 'doc-1');

    await waitFor(() => expect(screen.getByText('first paragraph')).toBeTruthy());
    expect(mockedContent).toHaveBeenCalledWith('doc-1', expect.objectContaining({
      page_no: 1,
      limit: 100,
      offset: 0,
    }));
    expect(pageMeta(container)).toContain(pagePosition(1, 3));
  });

  it('renders markup-like text literally without injecting HTML', async () => {
    const raw = '<script>alert(1)</script> & <b>bold</b>';
    mockedContent.mockResolvedValue(contentFixture({ chunks: [chunkFixture(1, 0, raw)] }));

    const { container } = renderReader('/?document=doc-1', 'doc-1');

    await waitFor(() => expect(screen.getByText(raw)).toBeTruthy());
    expect(container.querySelector('.knowledge-reader script')).toBeNull();
    expect(container.querySelector('.knowledge-reader b')).toBeNull();
  });

  it('moves between pages and batches with an accurate next_offset', async () => {
    mockedContent.mockResolvedValueOnce(
      contentFixture({ chunks: [chunkFixture(1, 0, 'page one')], next_offset: 100 }),
    );

    renderReader('/?document=doc-1&page=1', 'doc-1');
    await waitFor(() => expect(screen.getByText('page one')).toBeTruthy());

    mockedContent.mockResolvedValueOnce(contentFixture({ chunks: [chunkFixture(1, 100, 'page one b2')] }));
    fireEvent.click(screen.getByRole('button', { name: t('knowledge.reader.nextBatch') }));
    await waitFor(() => expect(screen.getByText('page one b2')).toBeTruthy());
    expect(mockedContent).toHaveBeenLastCalledWith('doc-1', expect.objectContaining({ offset: 100 }));
    expect(location()).toContain('offset=100');

    mockedContent.mockResolvedValueOnce(contentFixture({ chunks: [chunkFixture(1, 0, 'page one again')] }));
    fireEvent.click(screen.getByRole('button', { name: t('knowledge.reader.prevBatch') }));
    await waitFor(() => expect(screen.getByText('page one again')).toBeTruthy());
    expect(mockedContent).toHaveBeenLastCalledWith('doc-1', expect.objectContaining({ offset: 0 }));

    mockedContent.mockResolvedValueOnce(contentFixture({ chunks: [chunkFixture(2, 0, 'page two')] }));
    fireEvent.click(screen.getByRole('button', { name: t('knowledge.reader.nextPage') }));
    await waitFor(() => expect(screen.getByText('page two')).toBeTruthy());
    expect(mockedContent).toHaveBeenLastCalledWith('doc-1', expect.objectContaining({ page_no: 2 }));
    expect(location()).toContain('page=2');

    mockedContent.mockResolvedValueOnce(contentFixture({ chunks: [chunkFixture(1, 0, 'page one final')] }));
    fireEvent.click(screen.getByRole('button', { name: t('knowledge.reader.prevPage') }));
    await waitFor(() => expect(screen.getByText('page one final')).toBeTruthy());
    expect(mockedContent).toHaveBeenLastCalledWith('doc-1', expect.objectContaining({ page_no: 1 }));
  });

  it('submits the page form only on submit and rejects an invalid page', async () => {
    mockedContent.mockResolvedValue(contentFixture({ chunks: [chunkFixture(1, 0, 'page one')] }));

    renderReader('/?document=doc-1&page=1', 'doc-1');
    await waitFor(() => expect(mockedContent).toHaveBeenCalledTimes(1));

    const input = screen.getByLabelText(t('knowledge.reader.pageLabel'));
    fireEvent.change(input, { target: { value: '2' } });
    expect(mockedContent).toHaveBeenCalledTimes(1);

    fireEvent.change(input, { target: { value: '0' } });
    fireEvent.click(screen.getByRole('button', { name: t('knowledge.reader.go') }));
    expect(screen.getByRole('alert').textContent).toContain(t('knowledge.reader.invalidPageInput'));
    expect(mockedContent).toHaveBeenCalledTimes(1);

    mockedContent.mockResolvedValueOnce(contentFixture({ chunks: [chunkFixture(2, 0, 'page two')] }));
    fireEvent.change(input, { target: { value: '2' } });
    fireEvent.click(screen.getByRole('button', { name: t('knowledge.reader.go') }));
    await waitFor(() => expect(screen.getByText('page two')).toBeTruthy());
    expect(mockedContent).toHaveBeenLastCalledWith('doc-1', expect.objectContaining({ page_no: 2 }));
  });

  it('shows an empty page as a valid blank page', async () => {
    mockedContent.mockResolvedValueOnce(contentFixture({ chunks: [], next_offset: null }));

    renderReader('/?document=doc-1&page=2', 'doc-1');
    await waitFor(() => expect(screen.getByText(t('knowledge.reader.emptyPage'))).toBeTruthy());
    expect(mockedContent).toHaveBeenCalledWith('doc-1', expect.objectContaining({ page_no: 2 }));

    // The next page has text, so the message above was the real payload talking
    // and not a blanket placeholder.
    mockedContent.mockResolvedValueOnce(contentFixture({ chunks: [chunkFixture(2, 0, 'page two')] }));
    fireEvent.click(screen.getByRole('button', { name: t('knowledge.reader.prevPage') }));
    await waitFor(() => expect(screen.getByText('page two')).toBeTruthy());
    expect(screen.queryByText(t('knowledge.reader.emptyPage'))).toBeNull();
    expect(screen.getByText('\u8bba\u6587.pdf')).toBeTruthy();
  });

  it('reports the real document state after a 409, including needs_ocr', async () => {
    mockedContent.mockRejectedValue(
      new ApiError('not ready', { code: 'document.not_ready', status: 409 }),
    );
    mockedDocument
      .mockResolvedValueOnce(documentFixture({ state: 'processing' }))
      .mockResolvedValueOnce(documentFixture({ state: 'failed', error_code: 'needs_ocr' }));

    renderReader('/?document=doc-1', 'doc-1');

    await waitFor(() => expect(screen.getByText(t('knowledge.reader.state.processing'))).toBeTruthy());
    expect(mockedDocument).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByRole('button', { name: t('actions.refresh') }));
    await waitFor(() => expect(screen.getByText(t('knowledge.reader.error.needs_ocr'))).toBeTruthy());
    expect(screen.getByText(t('knowledge.reader.state.failed'))).toBeTruthy();
    expect(mockedDocument).toHaveBeenCalledTimes(2);
  });

  it('localizes a 404 document as missing', async () => {
    mockedContent.mockRejectedValue(
      new ApiError('missing', { code: 'document.not_found', status: 404 }),
    );

    renderReader('/?document=doc-404', 'doc-404');

    await waitFor(() => expect(screen.getByText(t('knowledge.reader.notFound'))).toBeTruthy());
    expect(screen.getByRole('alert')).toBeTruthy();
  });

  it('rejects a page beyond page_count instead of showing another page', async () => {
    mockedContent.mockResolvedValue(contentFixture({ document: documentFixture({ page_count: 3 }) }));

    renderReader('/?document=doc-1&page=5', 'doc-1');

    await waitFor(() => expect(screen.getByText(t('knowledge.reader.pageNotFound'))).toBeTruthy());

    mockedContent.mockResolvedValueOnce(contentFixture());
    fireEvent.click(screen.getByRole('button', { name: t('knowledge.reader.firstPage') }));
    await waitFor(() => expect(screen.getByText('first paragraph')).toBeTruthy());
    expect(mockedContent).toHaveBeenLastCalledWith('doc-1', expect.objectContaining({ page_no: 1 }));
  });

  it('focuses a citation only when the version matches and the ordinal is unique', async () => {
    mockedContent.mockResolvedValue(contentFixture({
      chunks: [chunkFixture(2, 100, 'paragraph 100'), chunkFixture(2, 105, 'cited paragraph')],
      document: documentFixture({ page_count: 3, version: 4 }),
    }));

    const { container } = renderReader('/?document=doc-1&page=2&paragraph=105&version=4', 'doc-1');

    await waitFor(() => expect(screen.getByText('cited paragraph')).toBeTruthy());
    const focused = container.querySelectorAll('.knowledge-reader-chunk.is-focused');
    expect(focused).toHaveLength(1);
    expect(focused[0].textContent).toBe('cited paragraph');
    expect(HTMLElement.prototype.scrollIntoView).toHaveBeenCalled();
    expect(mockedContent).toHaveBeenCalledWith('doc-1', expect.objectContaining({
      page_no: 2,
      offset: 100,
      limit: 100,
    }));
  });

  // A mismatch means the citation is not trustworthy: no highlight, and the
  // reader may only jump to the current version once the search settles.
  it('does not focus a stale citation after the document version changed', async () => {
    mockedContent.mockResolvedValue(contentFixture({
      chunks: [chunkFixture(2, 105, CITED_PARAGRAPH_TEXT)],
      document: documentFixture({ page_count: 3, version: 5 }),
    }));

    const { container } = renderReader('/?document=doc-1&page=2&paragraph=105&version=4', 'doc-1');

    await waitFor(() => expect(screen.getByText(t('knowledge.reader.versionChanged'))).toBeTruthy());
    expect(container.querySelectorAll('.knowledge-reader-chunk.is-focused')).toHaveLength(0);
    expect(screen.getByText(CITED_PARAGRAPH_TEXT)).toBeTruthy();

    fireEvent.click(screen.getByRole('button', { name: t('knowledge.reader.openCurrent') }));

    // Wait for the new address instead of settling for the pre-click DOM.
    await waitFor(() => expect(location()).toBe('?document=doc-1&page=2'));
    expect(location()).not.toContain('version');
    expect(location()).not.toContain('paragraph');
    expect(screen.queryByText(t('knowledge.reader.versionChanged'))).toBeNull();
  });

  it('honours an explicit offset over the derived citation window', async () => {
    mockedContent.mockResolvedValue(contentFixture({
      chunks: [chunkFixture(3, 50, 'explicit window'), chunkFixture(3, 205, 'cited paragraph')],
      document: documentFixture({ page_count: 3, version: 4 }),
    }));

    renderReader('/?document=doc-1&page=3&offset=50&paragraph=205&version=4', 'doc-1');

    await waitFor(() => expect(screen.getByText('explicit window')).toBeTruthy());
    expect(mockedContent).toHaveBeenCalledWith('doc-1', expect.objectContaining({ offset: 50 }));
  });

  it('does not bring back a stale page draft when returning to that page', async () => {
    mockedContent.mockResolvedValue(contentFixture({ chunks: [chunkFixture(1, 0, 'page one')] }));

    renderReader('/?document=doc-1&page=1', 'doc-1');
    await waitFor(() => expect(mockedContent).toHaveBeenCalledTimes(1));

    // Page 1 gets an invalid draft and a validation error.
    const input = screen.getByLabelText(t('knowledge.reader.pageLabel'));
    fireEvent.change(input, { target: { value: 'zz' } });
    fireEvent.click(screen.getByRole('button', { name: t('knowledge.reader.go') }));
    expect(screen.getByRole('alert').textContent).toContain(t('knowledge.reader.invalidPageInput'));

    // Navigate to page 2 through a real page control.
    mockedContent.mockResolvedValueOnce(contentFixture({ chunks: [chunkFixture(2, 0, 'page two')] }));
    fireEvent.click(screen.getByRole('button', { name: t('knowledge.reader.nextPage') }));
    await waitFor(() => expect(screen.getByText('page two')).toBeTruthy());

    mockedContent.mockResolvedValueOnce(contentFixture({ chunks: [chunkFixture(1, 0, 'page one again')] }));
    fireEvent.click(screen.getByRole('button', { name: t('knowledge.reader.prevPage') }));
    await waitFor(() => expect(screen.getByText('page one again')).toBeTruthy());

    expect((screen.getByLabelText(t('knowledge.reader.pageLabel')) as HTMLInputElement).value).toBe('1');
    expect(screen.queryByText(t('knowledge.reader.invalidPageInput'))).toBeNull();
  });

  it('reports a document 404 on a later page as a missing document', async () => {
    mockedContent.mockRejectedValue(
      new ApiError('missing', { code: 'document.not_found', status: 404 }),
    );

    renderReader('/?document=doc-1&page=2', 'doc-1');

    await waitFor(() => expect(screen.getByText(t('knowledge.reader.notFound'))).toBeTruthy());
    expect(screen.queryByText(t('knowledge.reader.pageNotFound'))).toBeNull();
    // A missing document is not a wrong page, so there is no "first page" offer.
    expect(screen.queryByRole('button', { name: t('knowledge.reader.firstPage') })).toBeNull();

    // The error panel must offer its own way out; the header button is a
    // separate control, so scope the query to the alert instead of the page.
    const panel = within(screen.getByRole('alert'));
    const back = panel.getByRole('button', { name: t('knowledge.reader.back') }) as HTMLButtonElement;
    expect(back.disabled).toBe(false);
  });

  it('keeps the real ready state and no not-ready copy after a 409 race', async () => {
    mockedContent.mockRejectedValueOnce(
      new ApiError('not ready', { code: 'document.not_ready', status: 409 }),
    );
    mockedDocument.mockResolvedValueOnce(documentFixture({ state: 'ready' }));

    renderReader('/?document=doc-1', 'doc-1');

    await waitFor(() => expect(screen.getByText(t('knowledge.reader.state.ready'))).toBeTruthy());
    expect(screen.queryByText(t('knowledge.reader.notReadyTitle'))).toBeNull();
    expect(mockedDocument).toHaveBeenCalledTimes(1);

    // The manual retry is the only recovery; no polling was added.
    mockedContent.mockResolvedValueOnce(contentFixture({ chunks: [chunkFixture(1, 0, 'page one')] }));
    fireEvent.click(screen.getByRole('button', { name: t('actions.refresh') }));
    await waitFor(() => expect(screen.getByText('page one')).toBeTruthy());
    expect(mockedContent).toHaveBeenCalledTimes(2);
  });

  it('warns clearly when the cited ordinal is not in the window', async () => {
    mockedContent.mockResolvedValue(contentFixture({
      chunks: [chunkFixture(3, 200, 'other paragraph')],
      document: documentFixture({ page_count: 3, version: 4 }),
    }));

    const { container } = renderReader('/?document=doc-1&page=3&paragraph=205&version=4', 'doc-1');

    await waitFor(() => expect(screen.getByText(t('knowledge.reader.paragraphNotFound'))).toBeTruthy());
    expect(container.querySelectorAll('.knowledge-reader-chunk.is-focused')).toHaveLength(0);
    // The window is derived once; there is no batch scan for a missing citation.
    expect(mockedContent).toHaveBeenCalledTimes(1);
  });

  it('reports invalid query parameters without requesting content', () => {
    renderReader('/?document=doc-1&page=abc&paragraph=-3', 'doc-1');

    expect(screen.getByText(t('knowledge.reader.invalidParams'))).toBeTruthy();
    expect(mockedContent).not.toHaveBeenCalled();
  });

  it('ignores a late success from the previous document', async () => {
    const old = deferred<DocumentContent>();
    mockedContent.mockImplementationOnce(() => old.promise);
    mockedContent.mockResolvedValueOnce(contentFixture({ chunks: [chunkFixture(1, 0, 'new document text')] }));

    const reader = renderReader('/?document=doc-1', 'doc-1');
    reader.switchTo('doc-2');
    await waitFor(() => expect(screen.getByText('new document text')).toBeTruthy());

    old.resolve(contentFixture({ chunks: [chunkFixture(1, 0, 'stale document text')] }));
    await act(async () => { await old.promise.catch(() => undefined); });
    expect(screen.queryByText('stale document text')).toBeNull();
    expect(screen.getByText('new document text')).toBeTruthy();
    expect(mockedDocument).not.toHaveBeenCalled();
  });

  it('ignores a late failure from the previous document', async () => {
    const old = deferred<DocumentContent>();
    mockedContent.mockImplementationOnce(() => old.promise);
    mockedContent.mockResolvedValueOnce(contentFixture({ chunks: [chunkFixture(1, 0, 'new document text')] }));

    const reader = renderReader('/?document=doc-1', 'doc-1');
    reader.switchTo('doc-2');
    await waitFor(() => expect(screen.getByText('new document text')).toBeTruthy());

    old.reject(new ApiError('not ready', { code: 'document.not_ready', status: 409 }));
    await act(async () => { await old.promise.catch(() => undefined); });
    expect(screen.getByText('new document text')).toBeTruthy();
    expect(mockedDocument).not.toHaveBeenCalled();
  });

  it('aborts the in-flight request on unmount', async () => {
    const pending = deferred<DocumentContent>();
    let signal: AbortSignal | undefined;
    mockedContent.mockImplementation((_id, options) => {
      signal = options?.signal;
      return pending.promise;
    });

    const reader = renderReader('/?document=doc-1', 'doc-1');
    await waitFor(() => expect(mockedContent).toHaveBeenCalledTimes(1));
    expect(signal?.aborted).toBe(false);

    reader.unmount();
    expect(signal?.aborted).toBe(true);
  });

  it('returns to the library keeping base and dropping the reader query', async () => {
    mockedContent.mockResolvedValue(contentFixture());

    renderReader('/?base=kb-9&document=doc-1&page=2&paragraph=100&version=4&offset=100', 'doc-1');
    await waitFor(() => expect(mockedContent).toHaveBeenCalledTimes(1));

    fireEvent.click(screen.getByRole('button', { name: t('knowledge.reader.back') }));

    // The click changes the address; wait for the final search, not for the
    // pre-click render.
    await waitFor(() => expect(location()).toBe('?base=kb-9'));
    expect(location()).not.toContain('document');
    expect(location()).not.toContain('paragraph');
  });

  it('does not warn in React for the citation jump', async () => {
    const error = vi.spyOn(console, 'error').mockImplementation(() => {});
    mockedContent.mockResolvedValue(contentFixture({
      chunks: [chunkFixture(2, 105, CITED_PARAGRAPH_TEXT)],
      document: documentFixture({ page_count: 3, version: 5 }),
    }));

    const { container } = renderReader('/?document=doc-1&page=2&paragraph=105&version=4', 'doc-1');
    await waitFor(() => expect(screen.getByText(t('knowledge.reader.versionChanged'))).toBeTruthy());

    fireEvent.click(screen.getByRole('button', { name: t('knowledge.reader.openCurrent') }));
    await waitFor(() => expect(location()).toBe('?document=doc-1&page=2'));
    // Settle the refetch the new address triggers before looking for warnings.
    await waitFor(() => expect(mockedContent).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(pageMeta(container)).toContain(pagePosition(2, 3)));

    expect(error).not.toHaveBeenCalled();
  });

  it('does not warn in React when leaving the reader', async () => {
    const error = vi.spyOn(console, 'error').mockImplementation(() => {});
    mockedContent.mockResolvedValue(contentFixture());

    const { container } = renderReader('/?base=kb-9&document=doc-1&page=2&offset=100', 'doc-1');
    await waitFor(() => expect(pageMeta(container)).toContain(pagePosition(2, 3)));

    fireEvent.click(screen.getByRole('button', { name: t('knowledge.reader.back') }));
    await waitFor(() => expect(location()).toBe('?base=kb-9'));
    // The reader keeps rendering the base library window at page 1; settle the
    // refetch that the new address triggers before looking for warnings.
    await waitFor(() => expect(mockedContent).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(pageMeta(container)).toContain(pagePosition(1, 3)));

    expect(error).not.toHaveBeenCalled();
  });
});
