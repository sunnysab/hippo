import { render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { SourceDetailPage } from './SourceDetailPage';

const apiGetMock = vi.fn();
const apiSendMock = vi.fn();

vi.mock('../../api', () => ({
  apiGet: (...args: unknown[]) => apiGetMock(...args),
  apiSend: (...args: unknown[]) => apiSendMock(...args),
  isAuthError: () => false,
}));

vi.mock('../../i18n', () => ({
  useI18n: () => ({ t: (key: string, fallback?: string) => fallback || key }),
}));

vi.mock('../../hooks/useToast', () => ({ useToast: () => ({ showToast: vi.fn() }) }));

// 登录面板有自己的轮询；这一页只关心它被放在了公众号来源下。
vi.mock('./LoginPanel', () => ({
  LoginPanel: () => <div data-testid="weixin-login" />,
}));

const renderAt = (source: string) =>
  render(
    <MemoryRouter initialEntries={[`/admin/source/${source}`]}>
      <Routes>
        <Route path="/admin/source/:source" element={<SourceDetailPage />} />
      </Routes>
    </MemoryRouter>,
  );

describe('SourceDetailPage', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    apiGetMock.mockImplementation((path: string) => {
      if (path === '/api/admin/media') {
        return Promise.resolve({ bilibili_cookie_set: true, proxy_bilibili: 'direct' });
      }
      return Promise.resolve({ accounts: [], total: 0 });
    });
  });

  it('puts the WeChat sign-in on the WeChat source, not on a page of its own', async () => {
    renderAt('weixin');

    expect(await screen.findByTestId('weixin-login')).toBeTruthy();
    // 扫码只解决公众号：别的来源上不该出现登录面板。
    expect(screen.queryByText('凭证')).toBeNull();
  });

  it('keeps the instance credential distinct from a follower-owned one', async () => {
    renderAt('bilibili');

    expect(await screen.findByText(/实例凭证 · 已配置/)).toBeTruthy();
    // 个人凭证在设置页维护，这里只说明会优先使用，不提供第二个输入框。
    expect(screen.queryByText(/我的凭证/)).toBeNull();
    expect(screen.getByText(/设置 → 来源凭证/)).toBeTruthy();
  });

  it('asks the backend to test the instance credential, not the caller-owned one', async () => {
    apiSendMock.mockResolvedValue({ ok: true, detail: '已登录', account: 'tester' });
    renderAt('bilibili');

    (await screen.findByRole('button', { name: '测试' })).click();

    await waitFor(() =>
      expect(apiSendMock).toHaveBeenCalledWith(
        '/api/source/credential/bilibili/test?scope=instance',
        'POST',
        {},
      ),
    );
  });

  it('says so when a source needs no credential at all', async () => {
    renderAt('podcast');

    expect(await screen.findByText(/播客 不需要凭证/)).toBeTruthy();
  });
});
