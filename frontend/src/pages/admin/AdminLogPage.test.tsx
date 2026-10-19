import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { I18nProvider } from '../../i18n';
import zhCN from '../../i18n/zh-CN.json';
import { AdminLogPage } from './AdminLogPage';

const apiGetMock = vi.fn();
const apiSendMock = vi.fn();

vi.mock('../../api', () => ({
  apiGet: (...args: unknown[]) => apiGetMock(...args),
  apiSend: (...args: unknown[]) => apiSendMock(...args),
}));

describe('AdminLogPage', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    apiGetMock.mockImplementation(async (path: string) => {
      if (path.startsWith('/api/admin/audit?')) {
        return { items: [], page: 1, page_size: 50, total: 0, pages: 0 };
      }
      if (path === '/api/admin/audit/action') return { items: [] };
      if (path.startsWith('/api/admin/log/link')) {
        return { available: true, url: 'https://signoz.example/logs?x=1' };
      }
      return {};
    });
  });

  it('reads the SignOz link with a GET for the selected window', async () => {
    const openMock = vi.spyOn(window, 'open').mockImplementation(() => null);

    render(
      <I18nProvider>
        <AdminLogPage />
      </I18nProvider>,
    );

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: zhCN['admin.log.openSignoz'] }));
    });

    // 这个端点是 GET + 查询参数；POST 过去只会拿到 405。
    expect(apiSendMock).not.toHaveBeenCalled();
    expect(apiGetMock).toHaveBeenCalledWith('/api/admin/log/link?minutes=60');
    await waitFor(() => {
      expect(openMock).toHaveBeenCalledWith('https://signoz.example/logs?x=1', '_blank', 'noopener');
    });
  });
});
