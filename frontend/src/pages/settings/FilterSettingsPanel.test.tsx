import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { I18nProvider } from '../../i18n';
import { emitRefresh } from '../../utils/events';
import { FilterSettingsPanel } from './FilterSettingsPanel';

const apiGetMock = vi.fn();
const apiSendMock = vi.fn();
const showToastMock = vi.fn();

vi.mock('../../api', () => ({
  apiGet: (...args: unknown[]) => apiGetMock(...args),
  apiSend: (...args: unknown[]) => apiSendMock(...args),
  isAuthError: () => false,
}));

vi.mock('../../hooks/useToast', () => ({
  useToast: () => ({ showToast: showToastMock }),
}));

const field = () => document.querySelector('#sync-article-exclude-keywords') as HTMLTextAreaElement;

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((nextResolve) => { resolve = nextResolve; });
  return { promise, resolve };
}

const renderPanel = () => render(
  <I18nProvider>
    <FilterSettingsPanel />
  </I18nProvider>,
);

describe('FilterSettingsPanel', () => {
  beforeEach(() => {
    apiGetMock.mockReset();
    apiSendMock.mockReset();
    showToastMock.mockReset();
  });

  it('reads and writes only the caller\'s own preferences', async () => {
    apiGetMock.mockResolvedValue({ timezone: 'Asia/Shanghai', article_exclude_keywords: 'seed' });
    apiSendMock.mockResolvedValue({ timezone: 'Asia/Shanghai', article_exclude_keywords: 'promo' });

    renderPanel();
    await waitFor(() => expect(field().value).toBe('seed'));

    fireEvent.change(field(), { target: { value: '  promo  ' } });
    fireEvent.click(document.querySelector('#btn-sync-filter-save')!);

    await waitFor(() => expect(apiSendMock).toHaveBeenCalledWith(
      '/api/settings/preferences',
      'PATCH',
      { article_exclude_keywords: 'promo' },
    ));
    const paths = [...apiGetMock.mock.calls, ...apiSendMock.mock.calls].map(([path]) => path);
    expect(paths).not.toContain('/api/settings');
  });

  it('applies only the newest read when a slower one lands late', async () => {
    const first = deferred<{ article_exclude_keywords: string }>();
    const second = deferred<{ article_exclude_keywords: string }>();
    apiGetMock.mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise);

    renderPanel();
    act(() => { emitRefresh(); });

    await act(async () => { second.resolve({ article_exclude_keywords: 'newer' }); });
    await waitFor(() => expect(field().value).toBe('newer'));

    await act(async () => { first.resolve({ article_exclude_keywords: 'stale' }); });
    expect(field().value).toBe('newer');
  });

  it('keeps an unsaved edit when the site-wide refresh fires', async () => {
    apiGetMock.mockResolvedValue({ article_exclude_keywords: 'seed' });

    renderPanel();
    await waitFor(() => expect(field().value).toBe('seed'));

    fireEvent.change(field(), { target: { value: 'draft' } });
    apiGetMock.mockClear();
    act(() => { emitRefresh(); });

    expect(field().value).toBe('draft');
    expect(apiGetMock).not.toHaveBeenCalled();
  });

  it('reports a failed load instead of showing an empty filter as if it were stored', async () => {
    apiGetMock.mockRejectedValue(new Error('offline'));

    renderPanel();

    await waitFor(() => expect(screen.getByText('阅读过滤加载失败，请刷新后重试。')).toBeTruthy());
  });
});
