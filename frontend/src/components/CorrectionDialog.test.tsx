import type { ComponentProps } from 'react';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { I18nProvider } from '../i18n';
import { CorrectionDialog } from './CorrectionDialog';

const listAccounts = vi.fn();
const listRules = vi.fn();
const save = vi.fn();
const preview = vi.fn();
vi.mock('../api/corrections', () => ({
  listCorrectionAccounts: () => listAccounts(), listCorrections: () => listRules(),
  saveCorrection: (...args: unknown[]) => save(...args), previewCorrection: (...args: unknown[]) => preview(...args),
  emitCorrectionsUpdated: () => window.dispatchEvent(new Event('hippo:corrections-updated')),
}));
const show = (props: Partial<ComponentProps<typeof CorrectionDialog>> = {}) => render(<I18nProvider><CorrectionDialog isOpen onClose={vi.fn()} onSaved={vi.fn()} {...props} /></I18nProvider>);
const account = { biz: 'a', nickname: 'Alice', source: 'youtube', avatar_url: '' };

describe('CorrectionDialog', () => {
  beforeEach(() => { listAccounts.mockReset().mockResolvedValue([account, { ...account, biz: 'b', nickname: 'Bob' }]); listRules.mockReset().mockResolvedValue([]); save.mockReset(); preview.mockReset(); });
  it('preserves a failed draft, allows retry, and selects multiple accounts', async () => {
    save.mockRejectedValueOnce(new Error('offline')).mockResolvedValueOnce({ id: 1, source_text: 'old', replacement_text: 'new', account_biz: ['a', 'b'], enabled: true });
    const onSaved = vi.fn(); const updated = vi.fn(); window.addEventListener('hippo:corrections-updated', updated);
    show({ onSaved });
    await screen.findByLabelText('原词');
    fireEvent.change(screen.getByLabelText('原词'), { target: { value: 'old' } });
    fireEvent.change(screen.getByLabelText('替换词'), { target: { value: 'new' } });
    fireEvent.click(screen.getByLabelText('指定账号'));
    await screen.findByLabelText(/Alice/);
    fireEvent.click(screen.getByLabelText(/Alice/)); fireEvent.click(screen.getByLabelText(/Bob/));
    await waitFor(() => expect((screen.getByRole('button', { name: '保存' }) as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(screen.getByRole('button', { name: '保存' }));
    await screen.findByText('offline');
    expect((screen.getByLabelText('替换词') as HTMLInputElement).value).toBe('new');
    fireEvent.click(screen.getByRole('button', { name: '保存' }));
    await waitFor(() => expect(onSaved).toHaveBeenCalled());
    expect(save).toHaveBeenLastCalledWith({ source_text: 'old', replacement_text: 'new', account_biz: ['a', 'b'], enabled: true }, undefined);
    expect(updated).toHaveBeenCalledOnce(); window.removeEventListener('hippo:corrections-updated', updated);
  });
  it('rejects identical text and an empty selected scope', async () => {
    show();
    fireEvent.change(screen.getByLabelText('原词'), { target: { value: 'same' } });
    fireEvent.change(screen.getByLabelText('替换词'), { target: { value: 'same' } });
    await screen.findByLabelText('指定账号');
    expect((screen.getByRole('button', { name: '保存' }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.change(screen.getByLabelText('替换词'), { target: { value: 'other' } });
    fireEvent.click(screen.getByLabelText('指定账号'));
    expect((screen.getByRole('button', { name: '保存' }) as HTMLButtonElement).disabled).toBe(true);
  });
  it('guides duplicates to editing instead of overwriting', async () => {
    listRules.mockResolvedValue([{ id: 4, source_text: 'old', replacement_text: 'saved', account_biz: [], enabled: true }]);
    show({ initialSource: 'old' });
    const button = await screen.findByRole('button', { name: '编辑已有规则' });
    fireEvent.click(button);
    expect((screen.getByLabelText('替换词') as HTMLInputElement).value).toBe('saved');
    expect(save).not.toHaveBeenCalled();
  });
  it('focuses replacement and closes on Escape', async () => {
    const onClose = vi.fn(); const view = show({ onClose });
    expect(document.activeElement).toBe(screen.getByLabelText('替换词'));
    fireEvent.click(screen.getByLabelText('指定账号'));
    await screen.findByLabelText(/Alice/);
    fireEvent.keyDown(document, { key: 'Escape' }); expect(onClose).toHaveBeenCalled();
    view.unmount();
  });
  it('allows removing an unavailable scope from an existing rule', async () => {
    show({ rule: { id: 3, source_text: 'old', replacement_text: 'new', account_biz: ['disabled', 'a'], enabled: true } });
    const unavailable = await screen.findByLabelText(/disabled.*账号未订阅/);
    expect((screen.getByRole('button', { name: '保存' }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(unavailable);
    await waitFor(() => expect((screen.getByRole('button', { name: '保存' }) as HTMLButtonElement).disabled).toBe(false));
  });
  it('ignores a preview response from an obsolete draft', async () => {
    let resolveFirst!: (value: unknown) => void;
    preview.mockReturnValueOnce(new Promise((resolve) => { resolveFirst = resolve; })).mockResolvedValue({ status: 'ok', match_count: 2, contexts: [] });
    show({ articleId: 10, initialSource: 'old' });
    fireEvent.change(screen.getByLabelText('替换词'), { target: { value: 'first' } });
    await waitFor(() => expect(preview).toHaveBeenCalledTimes(1));
    fireEvent.change(screen.getByLabelText('替换词'), { target: { value: 'second' } });
    await waitFor(() => expect(screen.getByText(/实际命中次数： 2/)).toBeTruthy());
    await act(async () => { resolveFirst({ status: 'ok', match_count: 99, contexts: [] }); });
    expect(screen.queryByText(/99/)).toBeNull();
  });
});
