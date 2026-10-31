import { useRef } from 'react';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { I18nProvider } from '../../i18n';
import { ArticlesProvider, useArticlesState, type ArticlePayload } from '../../store/articles';
import { ArticlePreview } from './ArticlePreview';

const get = vi.fn();
const send = vi.fn();
vi.mock('../../api', async () => ({ ...await vi.importActual<typeof import('../../api')>('../../api'), apiGet: (...args: unknown[]) => get(...args), apiSend: (...args: unknown[]) => send(...args) }));
vi.mock('../../hooks/useReaderSettings', () => ({ useReaderSettings: () => ({ config: { hideSmall: false, followPlayback: false, miniPlayer: false } }) }));
vi.mock('../../hooks/useToast', () => ({ useToast: () => ({ showToast: vi.fn() }) }));
vi.mock('./ReadingSidebar', () => ({ ReadingSidebar: () => null }));
vi.mock('./useArticleReferences', () => ({ useArticleReferences: () => ({ references: new Map(), failed: false, retry: vi.fn() }) }));

function payload(id: number, text: string): ArticlePayload {
  return {
    article: { id, title: `Article ${id}`, biz: 'a', source: 'youtube', read_at: null, starred_at: null } as ArticlePayload['article'],
    content: [{ type: 'video', url: '/api/media/7/video' }, { type: 'paragraph', transcript_source: 'asr', original_text: 'wrong', text, correction_spans: text === 'wrong' ? [] : [{ original_start: 0, original_end: 5, start: 0, end: text.length }] }],
    content_status: 'ready', content_updated_at: null, images: [],
  };
}

function Harness() {
  const { dispatch } = useArticlesState();
  const preview = useRef<HTMLDivElement>(null);
  return <><button onClick={() => dispatch({ type: 'SELECT_ARTICLE', id: 1, payload: payload(1, 'wrong') })}>First</button><button onClick={() => dispatch({ type: 'SELECT_ARTICLE', id: 2, payload: payload(2, 'second') })}>Second</button><ArticlePreview previewRef={preview} sidePanel="" onSidePanelToggle={() => {}} /></>;
}

describe('ArticlePreview correction refresh', () => {
  beforeEach(() => {
    get.mockReset(); send.mockReset();
    get.mockImplementation(async (path: string) => {
      if (path.includes('/annotation')) return { items: [] };
      if (path.startsWith('/api/account')) return { accounts: [{ biz: 'a', nickname: 'Alice', source: 'youtube', avatar_url: '', is_disabled: false }], total: 1 };
      if (path === '/api/correction') return { items: [] };
      return {};
    });
    send.mockResolvedValue({ id: 1, source_text: 'wrong', replacement_text: 'right', account_biz: ['a'], enabled: true });
  });

  it('prefills the reader action from an ASR selection and restores focus after close', async () => {
    const { container } = render(<I18nProvider><ArticlesProvider><Harness /></ArticlesProvider></I18nProvider>);
    fireEvent.click(screen.getByText('First'));
    const text = container.querySelector('[data-asr-original]')!.firstChild!;
    const range = document.createRange();
    range.setStart(text, 0); range.setEnd(text, 5);
    const selection = window.getSelection()!;
    selection.removeAllRanges(); selection.addRange(range);
    const button = screen.getByRole('button', { name: '添加纠错' });
    fireEvent.pointerDown(button);
    fireEvent.click(button);
    expect((screen.getByLabelText('原词') as HTMLInputElement).value).toBe('wrong');
    await screen.findByLabelText(/Alice/);
    fireEvent.click(screen.getByRole('button', { name: '取消' }));
    expect(document.activeElement).toBe(button);
    selection.removeAllRanges();
  });

  it('keeps an unsaved correction draft while the reading pane scrolls', async () => {
    const { container } = render(<I18nProvider><ArticlesProvider><Harness /></ArticlesProvider></I18nProvider>);
    fireEvent.click(screen.getByText('First'));
    fireEvent.click(screen.getByRole('button', { name: '添加纠错' }));
    await screen.findByLabelText(/Alice/);
    fireEvent.change(screen.getByLabelText('原词'), { target: { value: 'wrong' } });
    fireEvent.change(screen.getByLabelText('替换词'), { target: { value: 'unsaved' } });
    fireEvent.scroll(container.querySelector('#article-preview')!);
    expect((screen.getByLabelText('替换词') as HTMLInputElement).value).toBe('unsaved');
    expect(screen.getByRole('dialog')).toBeTruthy();
  });

  it('refreshes after saving without replacing the player, and ignores an old article response', async () => {
    const { container } = render(<I18nProvider><ArticlesProvider><Harness /></ArticlesProvider></I18nProvider>);
    fireEvent.click(screen.getByText('First'));
    const player = container.querySelector('video');
    expect(player).not.toBeNull();
    get.mockImplementation(async (path: string) => {
      if (path === '/api/article/1') return payload(1, 'right');
      if (path.includes('/annotation')) return { items: [] };
      if (path.startsWith('/api/account')) return { accounts: [{ biz: 'a', nickname: 'Alice', source: 'youtube', avatar_url: '' }], total: 1 };
      if (path === '/api/correction') return { items: [] };
      return {};
    });
    fireEvent.click(screen.getByRole('button', { name: '添加纠错' }));
    fireEvent.change(screen.getByLabelText('原词'), { target: { value: 'wrong' } });
    fireEvent.change(screen.getByLabelText('替换词'), { target: { value: 'right' } });
    await waitFor(() => expect((screen.getByRole('button', { name: '保存' }) as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(screen.getByRole('button', { name: '保存' }));
    await waitFor(() => expect(container.querySelector('[data-asr-original]')?.textContent).toBe('right'));
    expect(container.querySelector('video')).toBe(player);
    let resolveOld!: (value: ArticlePayload) => void;
    get.mockImplementation((path: string) => path === '/api/article/1' ? new Promise((resolve) => { resolveOld = resolve; }) : Promise.resolve({ items: [] }));
    act(() => window.dispatchEvent(new Event('hippo:corrections-updated')));
    fireEvent.click(screen.getByText('Second'));
    await act(async () => { resolveOld(payload(1, 'obsolete')); });
    expect(screen.getByText('Article 2')).toBeTruthy();
    expect(container.querySelector('[data-asr-original]')?.textContent).toBe('second');
  });
});
