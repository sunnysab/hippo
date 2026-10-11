import { fireEvent, render, screen, waitFor } from '@testing-library/react';
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

const open = (onSubscribed = vi.fn()) => {
  render(<AddSubscriptionModal isOpen onClose={vi.fn()} onSubscribed={onSubscribed} />);
  return onSubscribed;
};

const typeUrl = (value: string) => {
  fireEvent.change(screen.getByLabelText('订阅地址'), { target: { value } });
};

describe('AddSubscriptionModal', () => {
  beforeEach(() => {
    apiGetMock.mockReset();
    apiSendMock.mockReset();
  });

  it('resolves the pasted url into a channel before writing anything', async () => {
    apiGetMock.mockResolvedValue({
      source: 'bilibili',
      biz: 'bilibili:946974',
      nickname: '影视飓风',
      avatar_url: null,
      subscribed: false,
    });

    open();
    typeUrl('https://space.bilibili.com/946974/video');
    fireEvent.click(screen.getByText('解析'));

    await waitFor(() => expect(screen.getByText('影视飓风')).toBeTruthy());
    expect(screen.getByText(/哔哩哔哩/)).toBeTruthy();
    expect(apiGetMock.mock.calls[0][0]).toContain('/api/source/preview?url=');
    // 只是预览，还没写任何东西
    expect(apiSendMock).not.toHaveBeenCalled();
  });

  it('subscribes and tells the caller to refresh', async () => {
    apiGetMock.mockResolvedValue({
      source: 'youtube',
      biz: 'youtube:UC1',
      nickname: 'Fireship',
      avatar_url: null,
      subscribed: false,
    });
    apiSendMock.mockResolvedValue({});

    const onSubscribed = open();
    typeUrl('https://www.youtube.com/@Fireship');
    fireEvent.click(screen.getByText('解析'));
    await waitFor(() => expect(screen.getByText('订阅')).toBeTruthy());
    fireEvent.click(screen.getByText('订阅'));

    await waitFor(() => expect(apiSendMock).toHaveBeenCalledWith('/api/source/channel', 'POST', {
      url: 'https://www.youtube.com/@Fireship',
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
    typeUrl('https://feed.example/rss');
    fireEvent.click(screen.getByText('解析'));

    await waitFor(() => expect(screen.getByText('已经订阅过了。')).toBeTruthy());
    expect(screen.queryByText('订阅')).toBeNull();
  });

  it('shows why a url was refused', async () => {
    apiGetMock.mockRejectedValue(new Error('这个订阅里没有条目'));

    open();
    typeUrl('https://example.test/not-a-feed');
    fireEvent.click(screen.getByText('解析'));

    await waitFor(() => expect(screen.getByRole('alert').textContent).toBe('这个订阅里没有条目'));
  });
});
