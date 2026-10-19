import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter, useLocation } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import appSource from '../../App.tsx?raw';
import zhCN from '../../i18n/zh-CN.json';
import { I18nProvider } from '../../i18n';
import { ToastProvider } from '../../hooks/useToast';
import { ApiError } from '../../api';
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
  type DocumentContent,
  type KnowledgeBase,
  type KnowledgeDocument,
  type SearchHit,
  type SearchPage,
} from '../../api/knowledge';
import { TopBar } from '../../components/TopBar';
import { KnowledgePage } from './KnowledgePage';

vi.mock('../../api/knowledge', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../api/knowledge')>();
  return {
    ...actual,
    listKnowledgeBases: vi.fn(),
    getKnowledgeBase: vi.fn(),
    createKnowledgeBase: vi.fn(),
    renameKnowledgeBase: vi.fn(),
    deleteKnowledgeBase: vi.fn(),
    listDocuments: vi.fn(),
    deleteDocument: vi.fn(),
    retryDocument: vi.fn(),
    searchKnowledgeBase: vi.fn(),
    createDocument: vi.fn(),
    getDocument: vi.fn(),
    getDocumentContent: vi.fn(),
    uploadDocumentFile: vi.fn(),
  };
});

vi.mock('../../hooks/useAuth', () => ({
  useAuth: () => ({
    status: 'authenticated',
    user: {
      id: 1,
      username: 'tester',
      email: null,
      email_verified: true,
      role: 'admin',
      timezone: 'UTC',
    },
    isAdmin: true,
    refresh: vi.fn(),
    signIn: vi.fn(),
    signOut: vi.fn(),
  }),
}));

const mockListBases = vi.mocked(listKnowledgeBases);
const mockGetBase = vi.mocked(getKnowledgeBase);
const mockCreateBase = vi.mocked(createKnowledgeBase);
const mockRenameBase = vi.mocked(renameKnowledgeBase);
const mockDeleteBase = vi.mocked(deleteKnowledgeBase);
const mockListDocuments = vi.mocked(listDocuments);
const mockDeleteDocument = vi.mocked(deleteDocument);
const mockRetryDocument = vi.mocked(retryDocument);
const mockSearch = vi.mocked(searchKnowledgeBase);
const mockCreateDocument = vi.mocked(createDocument);
const mockGetDocument = vi.mocked(getDocument);
const mockContent = vi.mocked(getDocumentContent);
const mockUpload = vi.mocked(uploadDocumentFile);

/** React Router 6.30 warns about the React 19 future flags; opt in like the
 * reader tests do. The app's own Router is untouched. */
const ROUTER_FUTURE = {
  v7_startTransition: true,
  v7_relativeSplatPath: true,
};

// The dictionary stays the single source of the user-visible strings; a plain
// helper avoids rendering a Hook just to read one key.
const zhDictionary = zhCN as Record<string, string>;
const t = (key: string, fallback?: string): string => zhDictionary[key] ?? fallback ?? key;

const baseBody = (overrides: Partial<KnowledgeBase> = {}): KnowledgeBase => ({
  id: 'kb-1',
  name: 'Alpha',
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
  ...overrides,
});

const documentBody = (overrides: Partial<KnowledgeDocument> = {}): KnowledgeDocument => ({
  id: 'doc-1',
  knowledge_base_id: 'kb-1',
  filename: 'guide.pdf',
  mime_type: 'application/pdf',
  size_bytes: 2048,
  state: 'ready',
  attempts: 1,
  error_code: null,
  error_message: null,
  page_count: 2,
  version: 1,
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
  ...overrides,
});

const hitBody = (overrides: Partial<SearchHit> = {}): SearchHit => ({
  document_id: 'doc-1',
  knowledge_base_id: 'kb-1',
  filename: 'guide.pdf',
  version: 4,
  page_no: 2,
  ordinal: 5,
  text: 'first hit',
  chunk_type: 'paragraph',
  heading_level: null,
  ...overrides,
});

const contentBody = (document: KnowledgeDocument): DocumentContent => ({
  document,
  chunks: [{ page_no: 1, ordinal: 0, text: 'first paragraph', chunk_type: 'paragraph', heading_level: null }],
  next_offset: null,
});

const pdfFile = (name = 'guide.pdf') => new File(['%PDF-1.4'], name, { type: 'application/pdf' });

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

/** Flush the promise chains a settled API mock started, inside ``act``. */
const flush = async () => {
  await act(async () => {
    for (let i = 0; i < 12; i += 1) await Promise.resolve();
  });
};

const LocationProbe = () => {
  const { pathname, search } = useLocation();
  return <span data-testid="location">{`${pathname}${search}`}</span>;
};

const renderPage = (entry = '/knowledge') => render(
  <I18nProvider>
    <ToastProvider>
      <MemoryRouter initialEntries={[entry]} future={ROUTER_FUTURE}>
        <KnowledgePage />
        <LocationProbe />
      </MemoryRouter>
    </ToastProvider>
  </I18nProvider>,
);

const location = () => screen.getByTestId('location').textContent ?? '';

/** The hit label as the dictionary template renders it, 1-based page/paragraph. */
const hitMeta = (filename: string, page: number, ordinal: number) => t('knowledge.library.hitMeta')
  .replace('{filename}', filename)
  .replace('{page}', String(page))
  .replace('{ordinal}', String(ordinal));

beforeEach(() => {
  for (const mock of [
    mockListBases, mockGetBase, mockCreateBase, mockRenameBase, mockDeleteBase, mockListDocuments,
    mockDeleteDocument, mockRetryDocument, mockSearch, mockCreateDocument, mockGetDocument,
    mockContent, mockUpload,
  ]) {
    mock.mockReset();
  }
  mockListBases.mockResolvedValue([]);
  mockGetBase.mockImplementation(async (id) => baseBody({ id, name: `base-${id}` }));
  mockRenameBase.mockImplementation(async (_id, name) => baseBody({ name }));
  mockListDocuments.mockResolvedValue([]);
  mockSearch.mockResolvedValue({ hits: [], next_offset: null } satisfies SearchPage);
  mockCreateBase.mockImplementation(async (name) => baseBody({ id: 'kb-new', name }));
  mockDeleteBase.mockResolvedValue(undefined);
  mockDeleteDocument.mockResolvedValue(undefined);
  mockRetryDocument.mockImplementation(async (id) => documentBody({ id }));
  mockCreateDocument.mockImplementation(async (_baseId, input) => documentBody({
    id: `doc-${input.filename}`,
    filename: input.filename,
    state: 'pending_upload',
  }));
  mockGetDocument.mockImplementation(async (id) => documentBody({ id }));
  mockContent.mockImplementation(async (id) => contentBody(documentBody({ id, knowledge_base_id: 'kb-1' })));
  mockUpload.mockImplementation(async (id, file) => documentBody({
    id,
    filename: file.name,
    state: 'queued',
  }));
});

/** The dialog actions are clickable immediately after their control appears. */
const clickDialog = async (container: HTMLElement) => {
  const dialog = container.querySelector('.confirm-modal') as HTMLElement;
  await waitFor(() => expect(dialog).toBeTruthy());
  const confirm = within(dialog).getByRole('button', { name: t('common.delete') });
  // An immediately rejected delete lands its toast in the same turn as the
  // click, so the state update is settled inside act. A deferred delete simply
  // stays pending here, which keeps the slow-delete tests intact.
  await act(async () => {
    fireEvent.click(confirm);
  });
  await flush();
};

describe('KnowledgePage', () => {
  it('renders an empty library without requesting documents', async () => {
    renderPage();

    await waitFor(() => expect(screen.getByText(t('knowledge.library.empty'))).toBeTruthy());
    expect(screen.getByText(t('knowledge.upload.needBase'))).toBeTruthy();
    expect(mockListDocuments).not.toHaveBeenCalled();
    expect(mockGetBase).not.toHaveBeenCalled();
    expect(mockSearch).not.toHaveBeenCalled();
  });

  it('creates a base and selects it', async () => {
    mockListBases.mockResolvedValue([baseBody()]);
    const created = baseBody({ id: 'kb-new', name: 'Beta' });
    mockCreateBase.mockResolvedValue(created);

    renderPage('/knowledge?base=kb-1');
    await waitFor(() => expect(mockGetBase).toHaveBeenCalledWith('kb-1', expect.anything()));

    fireEvent.click(screen.getByRole('button', { name: t('knowledge.library.create') }));
    fireEvent.change(screen.getByLabelText(t('knowledge.library.createPrompt')), {
      target: { value: 'Beta' },
    });
    fireEvent.click(screen.getByRole('button', { name: t('common.save') }));

    await waitFor(() => expect(mockCreateBase).toHaveBeenCalledWith('Beta'));
    await waitFor(() => expect(mockGetBase).toHaveBeenCalledWith('kb-new', expect.anything()));
    expect(location()).toContain('base=kb-new');
  });

  it('keeps a base when its deletion fails and reports it', async () => {
    mockListBases.mockResolvedValue([baseBody()]);
    mockDeleteBase.mockRejectedValue(new ApiError('server exploded', { status: 500 }));

    const { container } = renderPage('/knowledge?base=kb-1');
    await waitFor(() => expect(screen.getByRole('button', { name: 'Alpha' })).toBeTruthy());

    fireEvent.click(screen.getByRole('button', { name: t('common.delete') }));
    await clickDialog(container);

    await waitFor(() => expect(screen.getByText(t('knowledge.error.deleteBaseFailed'))).toBeTruthy());
    expect(screen.getByRole('button', { name: 'Alpha' })).toBeTruthy();
    expect(location()).toContain('base=kb-1');
  });

  it('keeps a base when its rename fails', async () => {
    mockListBases.mockResolvedValue([baseBody()]);
    mockRenameBase.mockRejectedValue(new ApiError('rename blew up', { status: 500 }));

    renderPage('/knowledge?base=kb-1');
    await waitFor(() => expect(screen.getByRole('button', { name: 'Alpha' })).toBeTruthy());

    fireEvent.click(screen.getByRole('button', { name: t('knowledge.library.rename') }));
    fireEvent.change(screen.getByLabelText(t('knowledge.library.rename')), {
      target: { value: 'Alpha 2' },
    });
    fireEvent.click(screen.getByRole('button', { name: t('common.save') }));

    await waitFor(() => expect(screen.getByText(t('knowledge.error.renameFailed'))).toBeTruthy());
    expect(screen.getByRole('button', { name: 'Alpha' })).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Alpha 2' })).toBeNull();
  });

  it('renames a base and reloads its detail', async () => {
    // One local server value, flipped by the rename, so the list and the detail
    // agree before and after the write instead of the list being stale forever.
    let serverName = 'Alpha';
    mockListBases.mockImplementation(async () => [baseBody({ name: serverName })]);
    mockGetBase.mockImplementation(async (id) => baseBody({ id, name: serverName }));
    mockRenameBase.mockImplementation(async (_id, name) => {
      serverName = name;
      return baseBody({ name: serverName });
    });

    renderPage('/knowledge?base=kb-1');
    await waitFor(() => expect(screen.getByRole('button', { name: 'Alpha' })).toBeTruthy());
    await waitFor(() => expect(mockGetBase).toHaveBeenCalledWith('kb-1', expect.anything()));
    const before = mockGetBase.mock.calls.length;

    fireEvent.click(screen.getByRole('button', { name: t('knowledge.library.rename') }));
    fireEvent.change(screen.getByLabelText(t('knowledge.library.rename')), {
      target: { value: 'Alpha 2' },
    });
    fireEvent.click(screen.getByRole('button', { name: t('common.save') }));

    await waitFor(() => expect(mockRenameBase).toHaveBeenCalledWith('kb-1', 'Alpha 2'));
    // The detail is refetched from the server instead of trusting a late reply.
    await waitFor(() => expect(mockGetBase.mock.calls.length).toBeGreaterThan(before));
    await waitFor(() => expect(screen.getByRole('button', { name: 'Alpha 2' })).toBeTruthy());
  });

  it('keeps a document when its deletion fails', async () => {
    mockListBases.mockResolvedValue([baseBody()]);
    mockListDocuments.mockResolvedValue([documentBody({ filename: 'keep.pdf' })]);
    mockDeleteDocument.mockRejectedValue(new ApiError('delete blew up', { status: 500 }));

    const { container } = renderPage('/knowledge?base=kb-1');
    await waitFor(() => expect(screen.getByText('keep.pdf')).toBeTruthy());

    // The base toolbar carries a delete button too, so scope to the row.
    const row = container.querySelector('.knowledge-doc') as HTMLElement;
    fireEvent.click(within(row).getByRole('button', { name: t('common.delete') }));
    await clickDialog(container);

    await waitFor(() => expect(screen.getByText(t('knowledge.error.deleteDocumentFailed'))).toBeTruthy());
    expect(screen.getByText('keep.pdf')).toBeTruthy();
  });

  it('keeps the base that the user selected while a rename of another is slow', async () => {
    const slowRename = deferred<KnowledgeBase>();
    mockListBases.mockResolvedValue([
      baseBody({ id: 'kb-1', name: 'Alpha' }),
      baseBody({ id: 'kb-2', name: 'Beta' }),
    ]);
    mockGetBase.mockImplementation(async (id) => baseBody({ id, name: `base-${id}` }));
    mockRenameBase.mockImplementation(() => slowRename.promise);

    const { container } = renderPage('/knowledge?base=kb-1');
    await waitFor(() => expect(mockListDocuments).toHaveBeenCalledWith('kb-1', expect.anything()));
    await waitFor(() => expect(mockGetBase).toHaveBeenCalledWith('kb-1', expect.anything()));

    fireEvent.click(screen.getByRole('button', { name: t('knowledge.library.rename') }));
    fireEvent.change(screen.getByLabelText(t('knowledge.library.rename')), {
      target: { value: 'Alpha 2' },
    });
    fireEvent.click(screen.getByRole('button', { name: t('common.save') }));
    await waitFor(() => expect(mockRenameBase).toHaveBeenCalled());

    // The user moves to B while the PATCH of A is still in flight.
    fireEvent.click(screen.getByRole('button', { name: 'Beta' }));
    await waitFor(() => expect(mockGetBase).toHaveBeenCalledWith('kb-2', expect.anything()));

    slowRename.resolve(baseBody({ id: 'kb-1', name: 'Alpha 2' }));
    await flush();

    // The late rename invalidates the detail load instead of writing A back:
    // B stays selected and its documents keep loading.
    expect(location()).toContain('base=kb-2');
    expect(screen.queryByText(t('knowledge.reader.state.processing'))).toBeNull();
    expect(container.querySelector('.knowledge-alert.is-error')).toBeNull();
    expect(screen.queryByRole('button', { name: 'Alpha 2' })).toBeNull();
    await waitFor(() => expect(mockListDocuments).toHaveBeenCalledWith('kb-2', expect.anything()));
  });

  it('refreshes the detail when a slow rename outlives an A-B-A round trip', async () => {
    const slowRename = deferred<KnowledgeBase>();
    let serverName = 'Alpha';
    // The sidebar window holds both bases, so the A -> B -> A hop is real.
    mockListBases.mockImplementation(async () => [
      baseBody({ id: 'kb-1', name: serverName }),
      baseBody({ id: 'kb-2', name: 'Beta' }),
    ]);
    mockGetBase.mockImplementation(async (id) => (
      id === 'kb-2' ? baseBody({ id, name: 'Beta' }) : baseBody({ id, name: serverName })
    ));
    mockRenameBase.mockImplementation(() => slowRename.promise);

    const { container } = renderPage('/knowledge?base=kb-1');
    await waitFor(() => expect(mockGetBase).toHaveBeenCalledWith('kb-1', expect.anything()));
    await screen.findByRole('button', { name: 'Beta' });

    fireEvent.click(screen.getByRole('button', { name: t('knowledge.library.rename') }));
    fireEvent.change(screen.getByLabelText(t('knowledge.library.rename')), {
      target: { value: 'Alpha 2' },
    });
    fireEvent.click(screen.getByRole('button', { name: t('common.save') }));
    await waitFor(() => expect(mockRenameBase).toHaveBeenCalledWith('kb-1', 'Alpha 2'));

    // A -> B -> A while the PATCH of A is still in flight: the second hop is a
    // new navigation key, but the address is the renamed base again.
    fireEvent.click(screen.getByRole('button', { name: 'Beta' }));
    await waitFor(() => expect(location()).toContain('base=kb-2'));
    fireEvent.click(screen.getByRole('button', { name: 'Alpha' }));
    await waitFor(() => expect(location()).toContain('base=kb-1'));

    serverName = 'Alpha 2';
    slowRename.resolve(baseBody({ name: 'Alpha 2' }));
    await flush();

    // The stale detail of A is refetched instead of showing the old title under
    // the freshly renamed sidebar entry.
    await waitFor(() => expect(container.querySelector('.knowledge-main-title h2')?.textContent).toBe('Alpha 2'));
    expect(screen.queryByRole('button', { name: 'Alpha' })).toBeNull();
  });

  it('does not select a slow created base after the scope moved on', async () => {
    const slowCreate = deferred<KnowledgeBase>();
    mockListBases.mockResolvedValue([
      baseBody({ id: 'kb-1', name: 'Alpha' }),
      baseBody({ id: 'kb-2', name: 'Beta' }),
    ]);
    mockCreateBase.mockImplementationOnce(() => slowCreate.promise);

    const view = renderPage('/knowledge?base=kb-1');
    await waitFor(() => expect(mockGetBase).toHaveBeenCalledWith('kb-1', expect.anything()));

    fireEvent.click(screen.getByRole('button', { name: t('knowledge.library.create') }));
    fireEvent.change(screen.getByLabelText(t('knowledge.library.createPrompt')), {
      target: { value: 'Gamma' },
    });
    fireEvent.click(screen.getByRole('button', { name: t('common.save') }));
    await waitFor(() => expect(mockCreateBase).toHaveBeenCalledWith('Gamma'));

    fireEvent.click(screen.getByRole('button', { name: 'Beta' }));
    await waitFor(() => expect(mockGetBase).toHaveBeenCalledWith('kb-2', expect.anything()));

    slowCreate.resolve(baseBody({ id: 'kb-new', name: 'Gamma' }));
    await flush();
    expect(location()).toContain('base=kb-2');

    view.unmount();
    await flush();
  });

  it('still selects a created base inside the same scope', async () => {
    mockListBases.mockResolvedValue([baseBody()]);
    mockCreateBase.mockResolvedValue(baseBody({ id: 'kb-3', name: 'Delta' }));
    mockGetBase.mockImplementation(async (id) => baseBody({ id, name: `base-${id}` }));

    renderPage('/knowledge?base=kb-1');
    await waitFor(() => expect(mockGetBase).toHaveBeenCalledWith('kb-1', expect.anything()));

    fireEvent.click(screen.getByRole('button', { name: t('knowledge.library.create') }));
    fireEvent.change(screen.getByLabelText(t('knowledge.library.createPrompt')), {
      target: { value: 'Delta' },
    });
    fireEvent.click(screen.getByRole('button', { name: t('common.save') }));

    await waitFor(() => expect(mockCreateBase).toHaveBeenCalledWith('Delta'));
    await waitFor(() => expect(mockGetBase).toHaveBeenCalledWith('kb-3', expect.anything()));
    expect(location()).toContain('base=kb-3');
  });

  it('refreshes the sidebar of the deleted base without moving the user', async () => {
    const slowDelete = deferred<void>();
    let serverBases = [
      baseBody({ id: 'kb-1', name: 'Alpha' }),
      baseBody({ id: 'kb-2', name: 'Beta' }),
    ];
    mockListBases.mockImplementation(async () => serverBases);
    mockListDocuments.mockResolvedValue([]);
    mockDeleteBase.mockImplementation(async () => {
      await slowDelete.promise;
      serverBases = serverBases.filter((base) => base.id !== 'kb-1');
    });

    const { container } = renderPage('/knowledge?base=kb-1');
    await waitFor(() => expect(mockGetBase).toHaveBeenCalledWith('kb-1', expect.anything()));

    fireEvent.click(screen.getByRole('button', { name: t('common.delete') }));
    await clickDialog(container);
    await waitFor(() => expect(mockDeleteBase).toHaveBeenCalledWith('kb-1'));

    fireEvent.click(screen.getByRole('button', { name: 'Beta' }));
    await waitFor(() => expect(mockGetBase).toHaveBeenCalledWith('kb-2', expect.anything()));

    slowDelete.resolve();
    await flush();

    // The user stays on B while the sidebar drops the deleted A.
    expect(location()).toContain('base=kb-2');
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Alpha' })).toBeNull());
    expect(screen.getByRole('button', { name: 'Beta' })).toBeTruthy();
  });

  it('does not surface a late document retry in another base', async () => {
    const slowRetry = deferred<KnowledgeDocument>();
    mockListBases.mockResolvedValue([
      baseBody({ id: 'kb-1', name: 'Alpha' }),
      baseBody({ id: 'kb-2', name: 'Beta' }),
    ]);
    mockListDocuments.mockImplementation(async (baseId) => (
      baseId === 'kb-1'
        ? [documentBody({ id: 'doc-1', filename: 'broken.pdf', state: 'failed', error_code: 'processing_failed' })]
        : [documentBody({
          id: 'doc-b',
          filename: 'beta.pdf',
          knowledge_base_id: 'kb-2',
          state: 'failed',
          error_code: 'processing_failed',
        })]
    ));
    mockRetryDocument.mockImplementation(() => slowRetry.promise);

    renderPage('/knowledge?base=kb-1');
    await waitFor(() => expect(screen.getByText('broken.pdf')).toBeTruthy());

    fireEvent.click(screen.getByRole('button', { name: t('knowledge.upload.retry') }));
    await waitFor(() => expect(mockRetryDocument).toHaveBeenCalledWith('doc-1'));

    fireEvent.click(screen.getByRole('button', { name: 'Beta' }));
    await waitFor(() => expect(screen.getByText('beta.pdf')).toBeTruthy());

    // The pending_upload hint was prepared for A and must not appear in B.
    slowRetry.resolve(documentBody({ id: 'doc-1', state: 'pending_upload' }));
    await flush();
    expect(screen.queryByText(t('knowledge.upload.pendingFile').replace('{name}', 'broken.pdf'))).toBeNull();
    expect(screen.queryByText(t('knowledge.upload.chooseFile'))).toBeNull();
  });

  it('keeps the rows that are on screen when a same-base refresh fails', async () => {
    mockListBases.mockResolvedValue([baseBody()]);
    mockListDocuments.mockImplementation(async () => {
      if (mockListDocuments.mock.calls.length > 1) {
        throw new ApiError('refresh blew up', { status: 500 });
      }
      return [documentBody({ state: 'processing', filename: 'busy.pdf' })];
    });

    renderPage('/knowledge?base=kb-1');
    await waitFor(() => expect(screen.getByText('busy.pdf')).toBeTruthy());

    fireEvent.click(screen.getByRole('button', { name: t('actions.refresh') }));

    // The failure is reported without hiding the rows it could not replace.
    await waitFor(() => expect(screen.getByText(t('knowledge.library.documentsFailed'))).toBeTruthy());
    expect(screen.getByText('busy.pdf')).toBeTruthy();
  });

  it('drops the rows and stops polling when a same-base refresh is refused', async () => {
    vi.useFakeTimers();
    mockListBases.mockResolvedValue([baseBody()]);
    mockListDocuments.mockImplementation(async () => {
      if (mockListDocuments.mock.calls.length > 1) {
        throw new ApiError('login required', { code: 'AUTH_REQUIRED', status: 401 });
      }
      return [documentBody({ state: 'processing', filename: 'busy.pdf' })];
    });

    const { container } = renderPage('/knowledge?base=kb-1');
    await flush();
    await flush();
    expect(screen.getByText('busy.pdf')).toBeTruthy();

    fireEvent.click(screen.getByRole('button', { name: t('actions.refresh') }));
    await flush();

    // The window is no longer readable, so its rows and pagination are gone.
    expect(screen.queryByText('busy.pdf')).toBeNull();
    expect(screen.getByText(t('knowledge.library.documentsFailed'))).toBeTruthy();
    const docsNav = container.querySelector('.knowledge-main .knowledge-workspace') as HTMLElement;
    expect(within(docsNav).getByRole('button', { name: t('knowledge.library.nextWindow') })).toHaveProperty('disabled', true);
    expect(mockListBases).toHaveBeenCalledTimes(1);

    const calls = mockListDocuments.mock.calls.length;
    await act(async () => { await vi.advanceTimersByTimeAsync(15000); });
    await flush();
    // A refused window must not be polled again by a timer left running.
    expect(mockListDocuments).toHaveBeenCalledTimes(calls);
    vi.useRealTimers();
  });

  it('clears the hits when the search is refused', async () => {
    mockListBases.mockResolvedValue([baseBody()]);
    mockSearch.mockImplementationOnce(async () => ({ hits: [hitBody()], next_offset: 20 }));
    mockSearch.mockImplementationOnce(async () => {
      throw new ApiError('forbidden', { status: 403 });
    });

    renderPage('/knowledge?base=kb-1');
    await waitFor(() => expect(mockListDocuments).toHaveBeenCalled());

    fireEvent.change(screen.getByLabelText(t('knowledge.library.searchLabel')), { target: { value: 'needle' } });
    fireEvent.click(screen.getByRole('button', { name: t('knowledge.library.searchSubmit') }));
    await waitFor(() => expect(screen.getByText('first hit')).toBeTruthy());

    fireEvent.click(screen.getByRole('button', { name: t('knowledge.library.searchNext') }));

    await waitFor(() => expect(screen.getByText(t('knowledge.library.searchFailed'))).toBeTruthy());
    expect(screen.queryByText('first hit')).toBeNull();
  });

  it('does not re-select a deleted base after the user moved on', async () => {
    const slowDelete = deferred<void>();
    mockListBases.mockResolvedValue([
      baseBody({ id: 'kb-1', name: 'Alpha' }),
      baseBody({ id: 'kb-2', name: 'Beta' }),
    ]);
    mockListDocuments.mockResolvedValue([]);
    mockDeleteBase.mockImplementation(() => slowDelete.promise);

    const { container } = renderPage('/knowledge?base=kb-1');
    await waitFor(() => expect(mockGetBase).toHaveBeenCalledWith('kb-1', expect.anything()));

    fireEvent.click(screen.getByRole('button', { name: t('common.delete') }));
    await clickDialog(container);
    await waitFor(() => expect(mockDeleteBase).toHaveBeenCalledWith('kb-1'));

    fireEvent.click(screen.getByRole('button', { name: 'Beta' }));
    await waitFor(() => expect(mockGetBase).toHaveBeenCalledWith('kb-2', expect.anything()));

    slowDelete.resolve();
    await flush();

    // The completion refreshes the sidebar but does not select another base
    // once the address already points somewhere else.
    expect(location()).toContain('base=kb-2');
    expect(mockListBases.mock.calls.length).toBeGreaterThan(1);
  });

  it('hides rows past the window and enables the next one', async () => {
    mockListBases.mockResolvedValue([baseBody()]);
    mockListDocuments.mockImplementation(async (_baseId, options) => (
      options?.offset === 50
        ? [documentBody({ id: 'doc-50', filename: 'doc-50.pdf' })]
        : Array.from({ length: 51 }, (_, index) => documentBody({
          id: `doc-${index}`,
          filename: `doc-${index}.pdf`,
        }))
    ));

    const { container } = renderPage('/knowledge?base=kb-1');

    await waitFor(() => expect(container.querySelectorAll('.knowledge-doc')).toHaveLength(50));
    expect(screen.queryByText('doc-50.pdf')).toBeNull();
    expect(mockListDocuments).toHaveBeenCalledWith('kb-1', expect.objectContaining({ limit: 51, offset: 0 }));

    // The sidebar paginates itself, so the window control is scoped to the
    // document workspace instead of luckily picking one of the two buttons.
    const documentsSection = container.querySelector('.knowledge-main .knowledge-workspace') as HTMLElement;
    fireEvent.click(within(documentsSection).getByRole('button', { name: t('knowledge.library.nextWindow') }));

    await waitFor(() => expect(screen.getByText('doc-50.pdf')).toBeTruthy());
    expect(mockListDocuments).toHaveBeenLastCalledWith('kb-1', expect.objectContaining({ limit: 51, offset: 50 }));
    expect(location()).toContain('docs_offset=50');
  });

  it('ignores a late document window from a previous base', async () => {
    const stale = deferred<KnowledgeDocument[]>();
    mockListBases.mockResolvedValue([
      baseBody({ id: 'kb-1', name: 'Alpha' }),
      baseBody({ id: 'kb-2', name: 'Beta' }),
    ]);
    mockListDocuments.mockImplementation(async (baseId) => (
      baseId === 'kb-1'
        ? stale.promise
        : [documentBody({ id: 'doc-b', filename: 'beta.pdf', knowledge_base_id: 'kb-2' })]
    ));

    const { container } = renderPage('/knowledge?base=kb-1');
    await waitFor(() => expect(mockListDocuments).toHaveBeenCalledWith('kb-1', expect.anything()));

    fireEvent.click(screen.getByRole('button', { name: 'Beta' }));
    await waitFor(() => expect(screen.getByText('beta.pdf')).toBeTruthy());

    stale.resolve([documentBody({ id: 'doc-a', filename: 'alpha.pdf' })]);
    await flush();

    expect(screen.queryByText('alpha.pdf')).toBeNull();
    expect(container.querySelectorAll('.knowledge-doc')).toHaveLength(1);
  });

  it('queues multiple selected files and never calls an accepted upload ready', async () => {
    mockListBases.mockResolvedValue([baseBody()]);

    const { container } = renderPage('/knowledge?base=kb-1');
    await waitFor(() => expect(mockListDocuments).toHaveBeenCalled());

    const input = container.querySelector('.knowledge-drop .knowledge-file-input') as HTMLInputElement;
    fireEvent.change(input, { target: { files: [pdfFile('one.pdf'), pdfFile('two.pdf')] } });

    await waitFor(() => expect(mockUpload).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(screen.getAllByText(t('knowledge.upload.phase.uploaded'))).toHaveLength(2));
    expect(mockCreateDocument).toHaveBeenCalledTimes(2);
    expect(container.querySelectorAll('.knowledge-queue-item')).toHaveLength(2);
    // ``uploaded`` is a transport fact, not a readable document.
    expect(screen.queryByText(t('knowledge.reader.state.ready'))).toBeNull();
  });

  it('retries a failed document through retryDocument without uploading again', async () => {
    mockListBases.mockResolvedValue([baseBody()]);
    mockUpload.mockResolvedValue(documentBody({
      id: 'doc-guide.pdf',
      filename: 'guide.pdf',
      state: 'failed',
      error_code: 'needs_ocr',
    }));

    const { container } = renderPage('/knowledge?base=kb-1');
    await waitFor(() => expect(mockListDocuments).toHaveBeenCalled());

    const input = container.querySelector('.knowledge-drop .knowledge-file-input') as HTMLInputElement;
    fireEvent.change(input, { target: { files: [pdfFile()] } });

    await waitFor(() => expect(screen.getAllByText(t('knowledge.upload.phase.failed'))).toHaveLength(1));
    expect(mockUpload).toHaveBeenCalledTimes(1);

    mockGetDocument.mockResolvedValue(documentBody({ id: 'doc-guide.pdf', state: 'failed', error_code: 'needs_ocr' }));
    mockRetryDocument.mockResolvedValue(documentBody({ id: 'doc-guide.pdf', state: 'queued', attempts: 2 }));

    fireEvent.click(screen.getByRole('button', { name: t('knowledge.upload.retry') }));

    await waitFor(() => expect(screen.getAllByText(t('knowledge.upload.phase.uploaded'))).toHaveLength(1));
    // The retry goes through the real upload hook, which owns its abort signal.
    expect(mockRetryDocument).toHaveBeenCalledWith('doc-guide.pdf', expect.objectContaining({
      signal: expect.any(AbortSignal),
    }));
    // The bytes were already accepted, so the retry must not repeat the PUT.
    expect(mockUpload).toHaveBeenCalledTimes(1);
  });

  it('asks for the source file again when a retry returns pending_upload', async () => {
    mockListBases.mockResolvedValue([baseBody()]);
    mockListDocuments.mockResolvedValue([
      documentBody({ id: 'doc-1', state: 'failed', error_code: 'processing_failed', filename: 'guide.pdf' }),
    ]);
    mockRetryDocument.mockResolvedValue(documentBody({ id: 'doc-1', state: 'pending_upload' }));
    mockGetDocument.mockResolvedValue(documentBody({ id: 'doc-1', state: 'pending_upload' }));

    renderPage('/knowledge?base=kb-1');
    await waitFor(() => expect(screen.getByText('guide.pdf')).toBeTruthy());

    fireEvent.click(screen.getByRole('button', { name: t('knowledge.upload.retry') }));
    await waitFor(() => expect(screen.getByText(t('knowledge.upload.chooseFile'))).toBeTruthy());
    expect(mockRetryDocument).toHaveBeenCalledWith('doc-1');

    const label = screen.getByText(t('knowledge.upload.chooseFile')).closest('label') as HTMLElement;
    const input = label.querySelector('input') as HTMLInputElement;
    fireEvent.change(input, { target: { files: [pdfFile()] } });

    await waitFor(() => expect(mockUpload).toHaveBeenCalled());
    // The same record is resumed: a second file pick must not create a new one.
    expect(mockUpload.mock.calls[0][0]).toBe('doc-1');
    expect(mockCreateDocument).not.toHaveBeenCalled();
  });

  it('offers a status check for an uncertain row and still never re-uploads', async () => {
    mockListBases.mockResolvedValue([baseBody()]);
    mockUpload.mockRejectedValue(new ApiError('network', { code: 'NETWORK' }));
    mockGetDocument.mockResolvedValue(documentBody({
      id: 'doc-guide.pdf',
      state: 'uploading',
      filename: 'guide.pdf',
    }));

    const { container } = renderPage('/knowledge?base=kb-1');
    await waitFor(() => expect(mockListDocuments).toHaveBeenCalled());

    const input = container.querySelector('.knowledge-drop .knowledge-file-input') as HTMLInputElement;
    fireEvent.change(input, { target: { files: [pdfFile()] } });

    await waitFor(() => expect(screen.getAllByText(t('knowledge.upload.phase.uncertain'))).toHaveLength(1));
    expect(mockUpload).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByRole('button', { name: t('knowledge.upload.check') }));

    // The check reconciles with the server; the bytes are never sent again.
    await waitFor(() => expect(mockGetDocument).toHaveBeenCalledWith('doc-guide.pdf', expect.anything()));
    expect(mockUpload).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(screen.getAllByText(t('knowledge.upload.phase.uncertain'))).toHaveLength(1));
  });

  it('offers no status check when an uncertain row has no server record', async () => {
    mockListBases.mockResolvedValue([baseBody()]);
    // The create call dies with a coded 5xx, so the task has no document id.
    mockCreateDocument.mockRejectedValue(new ApiError('internal', {
      code: 'internal_error',
      status: 500,
    }));

    const { container } = renderPage('/knowledge?base=kb-1');
    await waitFor(() => expect(mockListDocuments).toHaveBeenCalled());

    const input = container.querySelector('.knowledge-drop .knowledge-file-input') as HTMLInputElement;
    fireEvent.change(input, { target: { files: [pdfFile()] } });

    await waitFor(() => expect(screen.getAllByText(t('knowledge.upload.phase.uncertain'))).toHaveLength(1));
    // Nothing to check and nothing to re-send: only an explanation and remove.
    expect(screen.queryByRole('button', { name: t('knowledge.upload.check') })).toBeNull();
    expect(screen.getByText(t('knowledge.upload.uncertainNoId'))).toBeTruthy();

    fireEvent.click(screen.getByRole('button', { name: t('knowledge.upload.remove') }));
    await waitFor(() => expect(container.querySelectorAll('.knowledge-queue-item')).toHaveLength(0));

    // The hint must not have posted a second record.
    expect(mockCreateDocument).toHaveBeenCalledTimes(1);
    expect(mockUpload).not.toHaveBeenCalled();
  });

  it('does not offer removal for a row that is still in flight', async () => {
    mockListBases.mockResolvedValue([baseBody()]);
    const slowUpload = deferred<KnowledgeDocument>();
    mockUpload.mockImplementation(() => slowUpload.promise);

    const { container } = renderPage('/knowledge?base=kb-1');
    await waitFor(() => expect(mockListDocuments).toHaveBeenCalled());

    const input = container.querySelector('.knowledge-drop .knowledge-file-input') as HTMLInputElement;
    fireEvent.change(input, { target: { files: [pdfFile()] } });
    await waitFor(() => expect(screen.getAllByText(t('knowledge.upload.phase.uploading')).length).toBeGreaterThan(0));

    expect(screen.queryByRole('button', { name: t('knowledge.upload.remove') })).toBeNull();

    slowUpload.resolve(documentBody({ id: 'doc-guide.pdf', filename: 'guide.pdf', state: 'queued' }));
    await flush();
    await waitFor(() => expect(screen.getByRole('button', { name: t('knowledge.upload.remove') })).toBeTruthy());
  });

  it('opens a search hit with its citation', async () => {
    mockListBases.mockResolvedValue([baseBody()]);
    const first = hitBody({ text: '<b>hit</b> & text' });
    const second = hitBody({ document_id: 'doc-9', filename: 'second.pdf', page_no: 7, ordinal: 41, version: 5 });
    mockSearch.mockImplementation(async (_baseId, _q, options) => (
      options?.offset === 20
        ? { hits: [second], next_offset: null }
        : { hits: [first], next_offset: 20 }
    ));

    const { container } = renderPage('/knowledge?base=kb-1');
    await waitFor(() => expect(mockListDocuments).toHaveBeenCalled());

    fireEvent.change(screen.getByLabelText(t('knowledge.library.searchLabel')), { target: { value: 'needle' } });
    fireEvent.click(screen.getByRole('button', { name: t('knowledge.library.searchSubmit') }));

    await waitFor(() => expect(mockSearch).toHaveBeenCalledWith('kb-1', 'needle', expect.objectContaining({
      limit: 20,
      offset: 0,
    })));
    await waitFor(() => expect(screen.getByText('<b>hit</b> & text')).toBeTruthy());
    expect(container.querySelector('.knowledge-hit b')).toBeNull();
    // The human label is 1-based while the citation stays 0-based: the hit is
    // page 2 ordinal 5, so the reader-facing label reads page 2 paragraph 6.
    expect(screen.getByText(hitMeta('guide.pdf', 2, 6))).toBeTruthy();

    fireEvent.click(screen.getByRole('button', { name: t('knowledge.library.searchNext') }));
    await waitFor(() => expect(mockSearch).toHaveBeenCalledWith('kb-1', 'needle', expect.objectContaining({
      offset: 20,
    })));
    const secondLabel = hitMeta('second.pdf', 7, 42);
    await waitFor(() => expect(screen.getByText(secondLabel)).toBeTruthy());

    fireEvent.click(screen.getByText(secondLabel));
    await waitFor(() => expect(location()).toContain('document=doc-9'));
    expect(location()).toContain('page=7');
    expect(location()).toContain('paragraph=41');
    expect(location()).toContain('version=5');
  });

  it('keeps the queue mounted while the reader is open', async () => {
    mockListBases.mockResolvedValue([baseBody()]);
    mockListDocuments.mockResolvedValue([documentBody({ id: 'doc-r', filename: 'ready.pdf' })]);
    mockContent.mockResolvedValue(contentBody(documentBody({ id: 'doc-r', filename: 'ready.pdf' })));

    const { container } = renderPage('/knowledge?base=kb-1');
    await waitFor(() => expect(mockListDocuments).toHaveBeenCalled());

    const fileInput = container.querySelector('.knowledge-drop .knowledge-file-input') as HTMLInputElement;
    fireEvent.change(fileInput, { target: { files: [pdfFile('queue.pdf')] } });
    await waitFor(() => expect(container.querySelectorAll('.knowledge-queue-item')).toHaveLength(1));

    fireEvent.click(screen.getByRole('button', { name: t('knowledge.library.open') }));

    await waitFor(() => expect(mockContent).toHaveBeenCalledWith('doc-r', expect.anything()));
    // The real reader is showing this document's fixture text and title, so the
    // queue surviving is a real coexistence, not an empty or replaced page.
    await waitFor(() => expect(screen.getByText('first paragraph')).toBeTruthy());
    expect(container.querySelector('.knowledge-reader-title')?.textContent).toBe('ready.pdf');
    expect(container.querySelectorAll('.knowledge-queue-item')).toHaveLength(1);
    expect(screen.getByText('queue.pdf')).toBeTruthy();
  });

  it('polls a non-terminal window once at a time and stops on unmount', async () => {
    vi.useFakeTimers();
    mockListBases.mockResolvedValue([baseBody()]);
    const pending = deferred<KnowledgeDocument[]>();
    let signal: AbortSignal | undefined;
    mockListDocuments.mockImplementation((_baseId, options) => {
      if (mockListDocuments.mock.calls.length > 1) {
        signal = options?.signal;
        return pending.promise;
      }
      return Promise.resolve([documentBody({ id: 'doc-p', state: 'processing' })]);
    });

    const view = renderPage('/knowledge?base=kb-1');
    await flush();
    await flush();
    expect(mockListDocuments).toHaveBeenCalledTimes(1);

    // One timer per window, and it only fires after the previous request settled.
    await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
    await flush();
    expect(mockListDocuments).toHaveBeenCalledTimes(2);

    // The second request is still in flight, so no third call may overlap it.
    await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
    await flush();
    expect(mockListDocuments).toHaveBeenCalledTimes(2);

    pending.resolve([documentBody({ id: 'doc-p', state: 'ready' })]);
    await flush();
    await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
    await flush();
    // A terminal window stops the loop.
    expect(mockListDocuments).toHaveBeenCalledTimes(2);

    view.unmount();
    expect(signal?.aborted).toBe(true);
    vi.useRealTimers();
  });
});

describe('knowledge navigation', () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it('exposes the knowledge tab and route', () => {
    render(
      <MemoryRouter initialEntries={['/knowledge']} future={ROUTER_FUTURE}>
        <I18nProvider>
          <TopBar topbarRef={{ current: null }} currentTab="knowledge" />
        </I18nProvider>
      </MemoryRouter>,
    );

    const tab = screen.getByRole('link', { name: t('nav.knowledge') });
    expect(tab.getAttribute('href')).toBe('/knowledge');
    // The route is mounted inside the authenticated shell, not as a public one.
    expect(appSource).toContain('path="/knowledge"');
    expect(appSource).toContain('<KnowledgePage />');
  });
});
