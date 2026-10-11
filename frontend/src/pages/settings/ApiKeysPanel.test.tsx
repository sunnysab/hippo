import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiKeysPanel } from './ApiKeysPanel';

const apiGetMock = vi.fn();
const apiSendMock = vi.fn();

vi.mock('../../api', () => ({
  apiGet: (path: string) => apiGetMock(path),
  apiSend: (path: string, method: string, body: unknown) => apiSendMock(path, method, body),
  isAuthError: () => false,
}));

vi.mock('../../hooks/useToast', () => ({ useToast: () => ({ showToast: vi.fn() }) }));
vi.mock('../../utils/clipboard', () => ({ copyToClipboard: vi.fn().mockResolvedValue(undefined) }));

describe('ApiKeysPanel', () => {
  beforeEach(() => {
    apiGetMock.mockReset();
    apiSendMock.mockReset();
    apiGetMock.mockResolvedValue({ keys: [], scopes: ['read', 'write'] });
  });

  it('shows the secret once, right after creating a key', async () => {
    apiSendMock.mockResolvedValue({ id: 1, name: 'CI', prefix: 'abcd1234', secret: 'hippo_secret' });

    render(<ApiKeysPanel />);
    await waitFor(() => expect(screen.getByText('还没有密钥。')).toBeTruthy());

    fireEvent.change(screen.getByLabelText('名称'), { target: { value: 'CI' } });
    fireEvent.click(screen.getByText('创建密钥'));

    await waitFor(() => expect(screen.getByText('hippo_secret')).toBeTruthy());
    expect(screen.getByText('这是唯一一次显示明文，请立刻保存。')).toBeTruthy();
    expect(apiSendMock).toHaveBeenCalledWith('/api/apikey', 'POST', { name: 'CI' });
  });

  it('lists existing keys by their prefix, never by a secret', async () => {
    apiGetMock.mockResolvedValue({
      keys: [
        {
          id: 7,
          name: '笔记脚本',
          prefix: 'abcd1234',
          scopes: ['read'],
          last_used_at: '2026-10-11T10:00:00+00:00',
          last_used_ip: '10.0.0.1',
          expires_at: null,
          created_at: '2026-10-10T10:00:00+00:00',
        },
      ],
    });

    const { container } = render(<ApiKeysPanel />);

    await waitFor(() => expect(screen.getByText('笔记脚本')).toBeTruthy());
    expect(screen.getByText('abcd1234…')).toBeTruthy();
    expect(container.textContent).not.toContain('hippo_');
  });

  it('revokes a key through its own row', async () => {
    apiGetMock.mockResolvedValue({
      keys: [
        { id: 7, name: 'CI', prefix: 'abcd1234', scopes: ['read'], last_used_at: null, expires_at: null },
      ],
    });
    apiSendMock.mockResolvedValue({});

    render(<ApiKeysPanel />);
    await waitFor(() => expect(screen.getByText('吊销')).toBeTruthy());
    fireEvent.click(screen.getByText('吊销'));

    await waitFor(() => expect(apiSendMock).toHaveBeenCalledWith('/api/apikey/7', 'DELETE', {}));
  });
});
