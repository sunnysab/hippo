import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { I18nProvider } from '../../i18n';
import { AdminLlmPage } from './AdminLlmPage';

const mocks = vi.hoisted(() => ({ get: vi.fn(), send: vi.fn(), toast: vi.fn() }));

vi.mock('../../api', () => ({ apiGet: mocks.get, apiSend: mocks.send }));
vi.mock('../../hooks/useToast', () => ({ useToast: () => ({ showToast: mocks.toast }) }));

const provider = {
  id: 1,
  name: 'Example provider',
  base_url: 'https://example.com/v1',
  api_key_masked: '***',
  model: 'example-model',
  is_default: true,
  enabled: true,
};

const renderPage = () => render(<I18nProvider><AdminLlmPage /></I18nProvider>);

beforeEach(() => {
  vi.clearAllMocks();
  mocks.get.mockResolvedValue({ items: [] });
  mocks.send.mockResolvedValue({});
});

describe('AdminLlmPage loading', () => {
  it('shows a failed load and retries with a loading state', async () => {
    mocks.get.mockRejectedValueOnce(new Error('Unavailable'));
    renderPage();

    expect((await screen.findByRole('alert')).textContent).toContain('加载 LLM 配置失败');
    expect(screen.queryByText('还没有配置 provider')).toBeNull();

    let resolveRetry!: (value: { items: typeof provider[] }) => void;
    mocks.get.mockImplementationOnce(() => new Promise((resolve) => {
      resolveRetry = resolve;
    }));
    fireEvent.click(screen.getByRole('button', { name: '重试' }));

    expect(screen.getByText('加载中…')).toBeTruthy();
    expect(screen.queryByRole('alert')).toBeNull();
    expect(screen.queryByText('还没有配置 provider')).toBeNull();

    await act(async () => {
      resolveRetry({ items: [provider] });
    });

    expect(await screen.findByText(provider.name)).toBeTruthy();
    expect(screen.queryByRole('alert')).toBeNull();
    expect(mocks.get).toHaveBeenCalledTimes(2);
  });

  it('shows the empty state only after a successful empty response', async () => {
    renderPage();

    expect(await screen.findByText('还没有配置 provider')).toBeTruthy();
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('reports a failed refresh after deletion and hides stale providers during retry', async () => {
    mocks.get.mockResolvedValueOnce({ items: [provider] });
    renderPage();
    await screen.findByText(provider.name);

    mocks.get.mockRejectedValueOnce(new Error('Unavailable'));
    fireEvent.click(screen.getByRole('button', { name: '删除' }));
    const dialog = screen.getByRole('dialog');
    fireEvent.click(within(dialog).getByRole('button', { name: '删除' }));

    expect((await screen.findByRole('alert')).textContent).toContain('加载 LLM 配置失败');
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    expect(mocks.send).toHaveBeenCalledWith('/api/llm/provider/1', 'DELETE', {});
    expect(screen.queryByText('还没有配置 provider')).toBeNull();
    expect(screen.queryByText(provider.name)).toBeNull();

    let resolveRetry!: (value: { items: typeof provider[] }) => void;
    mocks.get.mockImplementationOnce(() => new Promise((resolve) => {
      resolveRetry = resolve;
    }));
    fireEvent.click(screen.getByRole('button', { name: '重试' }));

    expect(screen.getByText('加载中…')).toBeTruthy();
    expect(screen.queryByText(provider.name)).toBeNull();
    expect(screen.queryByText('还没有配置 provider')).toBeNull();

    await act(async () => {
      resolveRetry({ items: [] });
    });

    expect(await screen.findByText('还没有配置 provider')).toBeTruthy();
    expect(screen.queryByRole('alert')).toBeNull();
  });
});
