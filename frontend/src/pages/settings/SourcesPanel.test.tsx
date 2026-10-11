import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { SourcesPanel } from './SourcesPanel';

const apiGetMock = vi.fn();
const apiSendMock = vi.fn();

vi.mock('../../api', () => ({
  apiGet: (path: string) => apiGetMock(path),
  apiSend: (path: string, method: string, body: unknown) => apiSendMock(path, method, body),
  isAuthError: () => false,
}));
vi.mock('../../hooks/useToast', () => ({ useToast: () => ({ showToast: vi.fn() }) }));

const field = (id: string) => document.querySelector(`#${id}`) as HTMLInputElement | HTMLTextAreaElement;

describe('SourcesPanel', () => {
  beforeEach(() => {
    apiGetMock.mockReset();
    apiSendMock.mockReset();
    apiGetMock.mockResolvedValue({ bilibili: { configured: true }, youtube: { configured: false } });
  });

  it('reports whether each source is configured, never the value', async () => {
    render(<SourcesPanel />);

    await waitFor(() => expect(screen.getByText(/哔哩哔哩 · 已配置/)).toBeTruthy());
    expect(screen.getByText(/YouTube · 未配置/)).toBeTruthy();
    // 两个输入框都是空的：值不回显。
    expect(field('source-credential-bilibili').value).toBe('');
    expect(field('source-credential-youtube').value).toBe('');
  });

  it('saves a pasted bilibili cookie', async () => {
    apiSendMock.mockResolvedValue({});
    render(<SourcesPanel />);
    await waitFor(() => expect(field('source-credential-bilibili')).toBeTruthy());

    fireEvent.change(field('source-credential-bilibili'), { target: { value: 'SESSDATA=abc' } });
    fireEvent.click(document.querySelector('#source-credential-save-bilibili')!);

    await waitFor(() => expect(apiSendMock).toHaveBeenCalledWith('/api/source/credential/bilibili', 'PUT', {
      secret: 'SESSDATA=abc',
    }));
  });

  it('offers a youtube field for the cookies.txt the script submits', async () => {
    apiSendMock.mockResolvedValue({});
    render(<SourcesPanel />);
    await waitFor(() => expect(field('source-credential-youtube')).toBeTruthy());

    fireEvent.change(field('source-credential-youtube'), {
      target: { value: '# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tSID\tabc\n' },
    });
    fireEvent.click(document.querySelector('#source-credential-save-youtube')!);

    // 前后空白会被去掉（结尾换行由服务端写文件时补回来）。
    await waitFor(() => expect(apiSendMock).toHaveBeenCalledWith('/api/source/credential/youtube', 'PUT', {
      secret: '# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tSID\tabc',
    }));
  });

  it('tests a credential and shows the account it was accepted as', async () => {
    apiSendMock.mockResolvedValue({
      source: 'bilibili',
      credential: 'user',
      ok: true,
      detail: '已登录，投稿列表接口不会再被风控挡下',
      account: '某位观众',
    });
    render(<SourcesPanel />);
    await waitFor(() => expect(field('source-credential-bilibili')).toBeTruthy());

    fireEvent.click(document.querySelector('#source-credential-test-bilibili')!);

    await waitFor(() => expect(screen.getByText(/可用（某位观众）/)).toBeTruthy());
    expect(apiSendMock).toHaveBeenCalledWith('/api/source/credential/bilibili/test?scope=user', 'POST', {});
  });

  it('shows why a credential was refused', async () => {
    apiSendMock.mockResolvedValue({
      source: 'youtube',
      credential: 'instance',
      ok: false,
      detail: 'YouTube 要求登录（bot 检查）：这个出口需要一份有效 Cookie',
      account: '',
    });
    render(<SourcesPanel />);
    await waitFor(() => expect(field('source-credential-youtube')).toBeTruthy());

    fireEvent.click(document.querySelector('#source-credential-test-youtube')!);

    await waitFor(() => expect(screen.getByText(/不可用.*bot 检查/)).toBeTruthy());
  });

  it('clears only the source that has a credential', async () => {
    apiSendMock.mockResolvedValue({});
    render(<SourcesPanel />);
    await waitFor(() => expect(field('source-credential-bilibili')).toBeTruthy());

    expect(document.querySelector('#source-credential-clear-bilibili')).not.toBeNull();
    expect(document.querySelector('#source-credential-clear-youtube')).toBeNull();
    fireEvent.click(document.querySelector('#source-credential-clear-bilibili')!);

    await waitFor(() => expect(apiSendMock).toHaveBeenCalledWith('/api/source/credential/bilibili', 'DELETE', {}));
  });
});
