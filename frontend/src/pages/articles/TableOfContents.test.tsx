import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { useRef } from 'react';
import { I18nProvider } from '../../i18n';
import zhCN from '../../i18n/zh-CN.json';
import { ArticlesProvider, type ArticlePayload } from '../../store/articles';
import { ArticleContent } from './ArticleContent';
import { TableOfContents } from './TableOfContents';
import { buildOutline, type ArticleOutline } from './outline';

const get = vi.fn();
const send = vi.fn();
vi.mock('../../api', async () => ({
  ...await vi.importActual<typeof import('../../api')>('../../api'),
  apiGet: (...args: unknown[]) => get(...args),
  apiSend: (...args: unknown[]) => send(...args),
}));

const version = '2026-10-19T00:00:00Z';
const result = (changes: Partial<ArticleOutline> = {}): ArticleOutline => ({
  state: 'missing', content_updated_at: version, sections: [], can_generate: true,
  model: null, updated_at: null, error_code: null, ...changes,
});
const article = (source = 'weixin', content?: ArticlePayload['content']): ArticlePayload => ({
  article: { id: 42, title: 'Test article', source, link: 'https://example.test/video' } as ArticlePayload['article'],
  content: content ?? [{ type: 'paragraph', text: 'Opening', t: 0 }, { type: 'paragraph', text: 'Ending', t: 120 }],
  content_status: 'ok', content_updated_at: version, images: [],
});
const close = vi.fn();
function Reader({ payload }: { payload: ArticlePayload }) {
  const ref = useRef<HTMLDivElement>(null);
  return <ArticlesProvider><div ref={ref} style={{ overflowY: 'auto' }}>
    <ArticleContent payload={payload} hideSmall={false} onImageContextMenu={vi.fn()} />
    <TableOfContents key={`${payload.article.id}:${payload.content_updated_at}`} payload={payload}
      items={buildOutline(payload.content)} containerRef={ref} onClose={close} />
  </div></ArticlesProvider>;
}
const mount = (payload = article()) => render(<I18nProvider><Reader payload={payload} /></I18nProvider>);
const ready = () => result({ state: 'ready', sections: [{ title: 'Main topic', start_block: 0, children: [{ title: 'Closing topic', start_block: 1 }] }] });

describe('article navigation outline', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    get.mockResolvedValue(result());
    send.mockResolvedValue(result({ state: 'queued' }));
    vi.stubGlobal('matchMedia', () => ({ matches: true }));
    HTMLElement.prototype.scrollTo = vi.fn();
    window.scrollTo = vi.fn();
    HTMLMediaElement.prototype.play = vi.fn().mockResolvedValue(undefined);
  });
  afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); });

  it.each(['weixin', 'bilibili', 'youtube', 'podcast', 'rss', 'future-source'])('offers manual generation for %s without headings', async (source) => {
    mount(article(source));
    const generate = await screen.findByRole('button', { name: zhCN['articles.outline.generate'] });
    await waitFor(() => expect(generate.hasAttribute('disabled')).toBe(false));
    expect(send).not.toHaveBeenCalled();
    fireEvent.click(generate);
    await waitFor(() => expect(send).toHaveBeenCalledWith('/api/article/42/outline', 'POST', {
      force: false, content_updated_at: version,
    }, expect.objectContaining({ signal: expect.any(AbortSignal) })));
  });

  it('keeps original headings as the default and never generates on open', async () => {
    mount(article('weixin', [{ type: 'heading', level: 2, text: 'Original chapter' }]));
    expect(screen.getByRole('button', { name: 'Original chapter' })).toBeTruthy();
    expect(screen.getByRole('button', { name: zhCN['articles.outline.original'] }).getAttribute('aria-pressed')).toBe('true');
    await waitFor(() => expect(get).toHaveBeenCalled());
    expect(send).not.toHaveBeenCalled();
  });

  it('loads cached chapters, collapses children, and navigates without playing', async () => {
    get.mockResolvedValue(ready());
    const { container } = mount(article('youtube', [
      { type: 'video', url: '/api/media/42/video' },
      { type: 'paragraph', text: 'Opening', t: 0 },
      { type: 'image', image_id: 99 },
      { type: 'paragraph', text: 'Ending', t: 120 },
    ]));
    const child = await screen.findByRole('button', { name: 'Closing topic' });
    const video = container.querySelector('video')!;
    fireEvent.click(child);
    expect(HTMLElement.prototype.scrollTo).toHaveBeenCalled();
    expect(video.currentTime).toBe(0);
    expect(HTMLMediaElement.prototype.play).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: `${zhCN['articles.outline.playAt']} 02:00` }));
    expect(video.currentTime).toBe(120);
    expect(HTMLMediaElement.prototype.play).toHaveBeenCalledOnce();
    fireEvent.click(screen.getByRole('button', { name: `${zhCN['articles.outline.collapse']} Main topic` }));
    expect(screen.queryByRole('button', { name: 'Closing topic' })).toBeNull();
    expect(screen.getByRole('button', { name: 'Main topic' }).getAttribute('aria-current')).toBe('location');
    expect(send).not.toHaveBeenCalled();
  });

  it('links external media timestamps, including zero, to the source', async () => {
    get.mockResolvedValue(ready());
    mount();
    const zero = await screen.findByRole('link', { name: `${zhCN['articles.outline.playAt']} 00:00` });
    expect(zero.getAttribute('href')).toBe('https://example.test/video');
    expect(screen.getByRole('link', { name: `${zhCN['articles.outline.playAt']} 02:00` }).getAttribute('href'))
      .toBe('https://example.test/video?t=120');
  });

  it('requests explicit regeneration only for a ready outline', async () => {
    get.mockResolvedValue(ready());
    mount();
    fireEvent.click(await screen.findByRole('button', { name: zhCN['articles.outline.regenerate'] }));
    await waitFor(() => expect(send.mock.calls[0][2].force).toBe(true));
  });

  it('never shows links to a different reader snapshot', async () => {
    get.mockResolvedValue({ ...ready(), content_updated_at: '2026-10-20T00:00:00Z' });
    mount();
    await screen.findByRole('button', { name: zhCN['articles.outline.reload'] });
    expect(screen.queryByRole('button', { name: 'Main topic' })).toBeNull();
    expect(screen.getByRole('button', { name: zhCN['articles.outline.regenerate'] }).hasAttribute('disabled')).toBe(true);
  });

  it('disables generation for image-only and media-only content', async () => {
    get.mockResolvedValue(result({ can_generate: false }));
    mount(article('bilibili', [{ type: 'video', url: '/api/media/42/video' }]));
    await screen.findByText(zhCN['articles.outline.noText']);
    expect(screen.getByRole('button', { name: zhCN['articles.outline.generate'] }).hasAttribute('disabled')).toBe(true);
  });

  it('keeps generation errors visible and permits retry', async () => {
    send.mockRejectedValue(new Error('No provider configured.'));
    mount();
    const generate = await screen.findByRole('button', { name: zhCN['articles.outline.generate'] });
    await waitFor(() => expect(generate.hasAttribute('disabled')).toBe(false));
    fireEvent.click(generate);
    expect((await screen.findByRole('alert')).textContent).toContain('No provider configured.');
    expect(generate.hasAttribute('disabled')).toBe(false);
  });

  it('aborts reads and stops polling when hidden', async () => {
    get.mockResolvedValue(result({ state: 'processing' }));
    const view = mount();
    await screen.findByText(zhCN['articles.outline.working']);
    const signal = get.mock.calls[0][1].signal as AbortSignal;
    view.unmount();
    expect(signal.aborted).toBe(true);
  });

  it('ignores a response from the article that was just replaced', async () => {
    let resolve: (value: ArticleOutline) => void = () => {};
    get.mockImplementationOnce(() => new Promise<ArticleOutline>((done) => { resolve = done; }));
    const view = mount();
    await waitFor(() => expect(get).toHaveBeenCalledOnce());
    const signal = get.mock.calls[0][1].signal as AbortSignal;
    view.rerender(<I18nProvider><Reader payload={{ ...article(), article: { ...article().article, id: 99 } }} /></I18nProvider>);
    await act(async () => { resolve(ready()); });
    expect(signal.aborted).toBe(true);
    expect(screen.queryByRole('button', { name: 'Main topic' })).toBeNull();
  });

  it('hides on close or Escape', async () => {
    mount();
    fireEvent.click(screen.getByRole('button', { name: zhCN['articles.outline.close'] }));
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(close).toHaveBeenCalledTimes(2);
    await waitFor(() => expect(get).toHaveBeenCalled());
  });

  it('polls a newly queued job to completion', async () => {
    const view = mount();
    const generate = await screen.findByRole('button', { name: zhCN['articles.outline.generate'] });
    await waitFor(() => expect(generate.hasAttribute('disabled')).toBe(false));
    get.mockResolvedValueOnce(result({ state: 'processing' })).mockResolvedValue(ready());
    vi.useFakeTimers();
    await act(async () => { fireEvent.click(generate); });
    await act(async () => { await vi.advanceTimersByTimeAsync(2600); });
    expect(screen.getByRole('button', { name: 'Main topic' })).toBeTruthy();
    view.unmount();
  });
});
