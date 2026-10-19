import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { AddSubscriptionModal } from './AddSubscriptionModal';

const apiGetMock = vi.fn();
const apiSendMock = vi.fn();

vi.mock('../../api', () => ({
  apiGet: (path: string) => apiGetMock(path),
  apiSend: (path: string, method: string, body: unknown) => apiSendMock(path, method, body),
  isAuthError: () => false,
}));
vi.mock('../../hooks/useToast', () => ({ useToast: () => ({ showToast: vi.fn() }) }));
vi.mock('../../utils/events', () => ({ emitRefresh: vi.fn() }));

const open = (onSubscribed = vi.fn(), groupId: number | null = 7) => {
  render(<AddSubscriptionModal onClose={vi.fn()} groupId={groupId} onSubscribed={onSubscribed} />);
  return onSubscribed;
};

const type = (value: string) => {
  fireEvent.change(screen.getByLabelText('订阅地址或公众号名称'), { target: { value } });
};

const hit = (overrides: Record<string, unknown> = {}) => ({
  biz: 'gh_abc123',
  nickname: '中投数研',
  alias: 'gh_abc',
  round_head_img: 'http://wx.qlogo.cn/head/132',
  avatar_url: '/api/account/search/gh_abc123/avatar',
  is_added: false,
  ...overrides,
});

describe('AddSubscriptionModal', () => {
  beforeEach(() => {
    apiGetMock.mockReset();
    apiSendMock.mockReset();
  });

  it('treats a URL as a link and resolves it into a channel', async () => {
    apiGetMock.mockResolvedValue({
      source: 'bilibili',
      biz: 'bilibili:946974',
      nickname: '影视飓风',
      avatar_url: null,
      subscribed: false,
    });

    open();
    type('https://space.bilibili.com/946974/video');
    fireEvent.click(screen.getByText('解析'));

    await waitFor(() => expect(screen.getByText('影视飓风')).toBeTruthy());
    expect(screen.getByText(/哔哩哔哩/)).toBeTruthy();
    expect(apiGetMock.mock.calls[0][0]).toContain('/api/source/preview?url=');
    // 只是预览，还没写任何东西
    expect(apiSendMock).not.toHaveBeenCalled();
  });

  it('subscribes into the group the reader is looking at', async () => {
    apiGetMock.mockResolvedValue({
      source: 'youtube',
      biz: 'youtube:UC1',
      nickname: 'Fireship',
      avatar_url: null,
      subscribed: false,
    });
    apiSendMock.mockResolvedValue({});

    const onSubscribed = open();
    type('https://www.youtube.com/@Fireship');
    fireEvent.click(screen.getByText('解析'));
    await waitFor(() => expect(screen.getByText('订阅')).toBeTruthy());
    fireEvent.click(screen.getByText('订阅'));

    await waitFor(() => expect(apiSendMock).toHaveBeenCalledWith('/api/source/channel', 'POST', {
      url: 'https://www.youtube.com/@Fireship',
      group_id: 7,
    }));
    await waitFor(() => expect(onSubscribed).toHaveBeenCalled());
  });

  it('says so instead of offering to subscribe twice', async () => {
    apiGetMock.mockResolvedValue({
      source: 'podcast',
      biz: 'podcast:abc',
      nickname: '一档播客',
      avatar_url: null,
      subscribed: true,
    });

    open();
    type('https://feed.example/rss');
    fireEvent.click(screen.getByText('解析'));

    await waitFor(() => expect(screen.getByText('已经订阅过了。')).toBeTruthy());
    expect(screen.queryByText('订阅')).toBeNull();
  });

  it('shows why a url was refused', async () => {
    apiGetMock.mockRejectedValue(new Error('这个订阅里没有条目'));

    open();
    type('https://example.test/not-a-feed');
    fireEvent.click(screen.getByText('解析'));

    await waitFor(() => expect(screen.getByRole('alert').textContent).toBe('这个订阅里没有条目'));
  });

  it('reads plain text as a name and searches public accounts', async () => {
    apiGetMock.mockResolvedValue({ results: [hit()], page: 1, page_size: 10, total: 1 });

    open();
    type('中投数研');

    await waitFor(() => expect(apiGetMock).toHaveBeenCalled());
    expect(apiGetMock.mock.calls[0][0]).toContain('/api/account/search?q=');
    await waitFor(() => expect(screen.getByText('中投数研')).toBeTruthy());
  });

  it('adds the searched account to the current group and keeps the dialog open', async () => {
    apiGetMock.mockResolvedValue({ results: [hit()] });
    apiSendMock.mockResolvedValue({});
    const onSubscribed = open();

    type('中投数研');
    await waitFor(() => expect(screen.getByText('中投数研')).toBeTruthy());
    fireEvent.click(screen.getByText('添加'));

    await waitFor(() => expect(apiSendMock).toHaveBeenCalledWith('/api/account', 'POST', {
      biz: 'gh_abc123',
      nickname: '中投数研',
      alias: 'gh_abc',
      round_head_img: 'http://wx.qlogo.cn/head/132',
      group_id: 7,
    }));
    await waitFor(() => expect(onSubscribed).toHaveBeenCalled());
    // 名字搜索是连续添加，不关弹窗
    expect(screen.getByText('已添加')).toBeTruthy();
  });

  it('does not search on a single character', async () => {
    open();
    type('中');

    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 400)); });
    expect(apiGetMock).not.toHaveBeenCalled();
  });
});
