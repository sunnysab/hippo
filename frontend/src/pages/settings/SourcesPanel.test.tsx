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

describe('SourcesPanel', () => {
  beforeEach(() => {
    apiGetMock.mockReset();
    apiSendMock.mockReset();
  });

  it('shows whether a credential is already configured, never the value', async () => {
    apiGetMock.mockResolvedValue({ bilibili: { configured: true } });

    const { container } = render(<SourcesPanel />);

    await waitFor(() => expect(screen.getByText(/已配置/)).toBeTruthy());
    expect((container.querySelector('#source-credential') as HTMLInputElement).value).toBe('');
  });

  it('saves the pasted cookie', async () => {
    apiGetMock.mockResolvedValue({ bilibili: { configured: false } });
    apiSendMock.mockResolvedValue({});

    render(<SourcesPanel />);
    await waitFor(() => expect(screen.getByText('保存')).toBeTruthy());

    fireEvent.change(document.querySelector('#source-credential')!, { target: { value: 'SESSDATA=abc' } });
    fireEvent.click(screen.getByText('保存'));

    await waitFor(() => expect(apiSendMock).toHaveBeenCalledWith('/api/source/credential/bilibili', 'PUT', {
      secret: 'SESSDATA=abc',
    }));
  });

  it('clears a configured cookie', async () => {
    apiGetMock.mockResolvedValue({ bilibili: { configured: true } });
    apiSendMock.mockResolvedValue({});

    render(<SourcesPanel />);
    await waitFor(() => expect(screen.getByText('清除')).toBeTruthy());
    fireEvent.click(screen.getByText('清除'));

    await waitFor(() => expect(apiSendMock).toHaveBeenCalledWith('/api/source/credential/bilibili', 'DELETE', {}));
  });

  it('offers nothing to clear when nothing is configured', async () => {
    apiGetMock.mockResolvedValue({ bilibili: { configured: false } });

    render(<SourcesPanel />);

    await waitFor(() => expect(screen.getByText(/B站 Cookie/)).toBeTruthy());
    expect(screen.queryByText('清除')).toBeNull();
  });
});
