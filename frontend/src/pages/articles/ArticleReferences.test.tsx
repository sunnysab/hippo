import { act, fireEvent, render, renderHook, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { ArticlePayload } from '../../store/articles';
import { ArticleContent } from './ArticleContent';
import { useArticleReferences, type ArticleReference } from './useArticleReferences';

const get = vi.fn();
const send = vi.fn();
vi.mock('../../api', async () => ({
  ...await vi.importActual<typeof import('../../api')>('../../api'),
  apiGet: (...args: unknown[]) => get(...args),
  apiSend: (...args: unknown[]) => send(...args),
}));

const url = 'https://mp.weixin.qq.com/s?__biz=Mz1&mid=123&idx=2&sn=abc';
const reference = (fields: Partial<ArticleReference> = {}): ArticleReference => ({
  url, status: 'ready', article_id: 2, title: 'Stored title',
  canonical_url: 'https://mp.weixin.qq.com/s/slug', ...fields,
});
const payload = (text = `**1、[Original label](${url})**`, id = 1): ArticlePayload => ({
  article: { id, title: 'Title', source: 'weixin' } as ArticlePayload['article'],
  content: [{ type: 'paragraph', text }], content_status: 'ok', content_updated_at: 'v1', images: [],
});
const content = (value = payload()) => render(<ArticleContent payload={value} hideSmall={false}
  followPlayback={false} miniPlayer={false} onImageContextMenu={vi.fn()} />);

beforeEach(() => {
  get.mockReset().mockResolvedValue({ items: [reference()] });
  send.mockReset().mockResolvedValue({ items: [reference()] });
});
afterEach(() => { vi.useRealTimers(); });

describe('article reference rendering', () => {
  it('keeps a bold link label and opens the archived article without the old group filter', async () => {
    const { container } = content();
    await waitFor(() => expect(screen.getByRole('link', { name: 'Original label' }).getAttribute('href'))
      .toBe('#/articles?article=2'));
    expect(container.querySelector('strong a')?.textContent).toBe('Original label');
    expect(screen.getByRole('link', { name: 'Original label' }).getAttribute('target')).toBeNull();
    expect(screen.getByRole('link', { name: 'Open original' }).getAttribute('href'))
      .toBe('https://mp.weixin.qq.com/s/slug');
    expect(send.mock.calls[0][0]).toBe('/api/article/1/reference');
    expect(send.mock.calls[0][2]).toEqual({});
  });

  it('replaces a bare URL with the stored title', async () => {
    content(payload(url));
    await screen.findByRole('link', { name: 'Stored title' });
    expect(screen.queryByText(url)).toBeNull();
  });

  it('uses the href on a link block and preserves linked images', async () => {
    const value = payload();
    value.content = [{ type: 'link', href: url, text: 'Link block' },
      { type: 'image', image_id: 9, href: url, alt: 'Linked image' }];
    const { container } = content(value);
    await waitFor(() => expect(screen.getByRole('link', { name: 'Link block' }).getAttribute('href'))
      .toBe('#/articles?article=2'));
    expect(container.querySelector('figure a img')?.getAttribute('src')).toBe('/api/image/9');
    expect(screen.getByRole('link', { name: 'Linked image' }).getAttribute('href')).toBe('#/articles?article=2');
  });

  it('preserves the source URL while a body is pending', async () => {
    send.mockResolvedValue({ items: [reference({ status: 'pending', article_id: null, title: null })] });
    content();
    await screen.findByText('Queued');
    expect(screen.getByRole('link', { name: 'Original label' }).getAttribute('href')).toBe(url);
    expect(screen.getByRole('link', { name: 'Original label' }).getAttribute('target')).toBe('_blank');
  });

  it('retries only the clicked failed reference', async () => {
    send.mockResolvedValue({ items: [reference({ status: 'failed', article_id: null })] });
    content();
    fireEvent.click(await screen.findByRole('button', { name: 'Retry' }));
    await waitFor(() => expect(send).toHaveBeenCalledTimes(2));
    expect(send.mock.calls[1][2]).toEqual({ retry_url: url });
  });

  it('never requests code examples or renders unsafe block hrefs', () => {
    const value = payload();
    value.content = [{ type: 'code', text: url }, { type: 'link', href: 'javascript:alert(1)', text: 'Unsafe' }];
    content(value);
    expect(get).not.toHaveBeenCalled();
    expect(screen.queryByRole('link')).toBeNull();
  });
});

describe('reference lookup lifecycle', () => {
  it('waits for POST to establish access before publishing ready links', async () => {
    let finish!: (answer: object) => void;
    send.mockImplementation(() => new Promise(resolve => { finish = resolve; }));
    const { result } = renderHook(() => useArticleReferences(payload()));
    await waitFor(() => expect(send).toHaveBeenCalledTimes(1));
    expect(result.current.references.size).toBe(0);
    await act(async () => { finish({ items: [reference()] }); });
    expect(result.current.references.get(url)?.article_id).toBe(2);
  });

  it('polls a pending job, stops when ready and aborts on unmount', async () => {
    vi.useFakeTimers();
    const pending = reference({ status: 'pending', article_id: null });
    get.mockResolvedValueOnce({ items: [pending] }).mockResolvedValue({ items: [reference()] });
    send.mockResolvedValue({ items: [pending] });
    const { result, unmount } = renderHook(() => useArticleReferences(payload()));
    await act(async () => {});
    expect(result.current.references.get(url)?.status).toBe('pending');
    expect(get).toHaveBeenCalledTimes(1);
    await act(async () => { await vi.advanceTimersByTimeAsync(3000); });
    expect(result.current.references.get(url)?.status).toBe('ready');
    await act(async () => { await vi.advanceTimersByTimeAsync(9000); });
    expect(get).toHaveBeenCalledTimes(2);
    const signal = get.mock.calls[0][1].signal as AbortSignal;
    unmount();
    expect(signal.aborted).toBe(true);
    expect(vi.getTimerCount()).toBe(0);
  });

  it('clears a pending poll when leaving the article', async () => {
    vi.useFakeTimers();
    send.mockResolvedValue({ items: [reference({ status: 'pending', article_id: null })] });
    const { unmount } = renderHook(() => useArticleReferences(payload()));
    await act(async () => {});
    unmount();
    await act(async () => { await vi.advanceTimersByTimeAsync(6000); });
    expect(get).toHaveBeenCalledTimes(1);
    expect(vi.getTimerCount()).toBe(0);
  });

  it('ignores an old response after switching articles', async () => {
    let finish!: (answer: object) => void;
    get.mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
    const { result, rerender } = renderHook(({ value }) => useArticleReferences(value),
      { initialProps: { value: payload() } });
    const oldSignal = get.mock.calls[0][1].signal as AbortSignal;
    rerender({ value: payload(undefined, 9) });
    await waitFor(() => expect(result.current.references.get(url)?.article_id).toBe(2));
    expect(oldSignal.aborted).toBe(true);
    await act(async () => { finish({ items: [reference({ article_id: 100 })] }); });
    expect(result.current.references.get(url)?.article_id).toBe(2);
    expect(send).toHaveBeenCalledTimes(1);
    expect(send.mock.calls[0][0]).toBe('/api/article/9/reference');
  });

  it('exposes a lookup failure without leaving an unhandled rejection', async () => {
    get.mockRejectedValue(new Error('Unavailable'));
    const { result } = renderHook(() => useArticleReferences(payload()));
    await waitFor(() => expect(result.current.failed).toBe(true));
    expect(send).not.toHaveBeenCalled();
  });

  it('does not POST when the server finds no real article references', async () => {
    get.mockResolvedValue({ items: [] });
    renderHook(() => useArticleReferences(payload()));
    await act(async () => {});
    expect(send).not.toHaveBeenCalled();
  });
});
