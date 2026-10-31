import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { I18nProvider } from '../../i18n';
import { CorrectionsPanel } from './CorrectionsPanel';

const list = vi.fn(); const accounts = vi.fn(); const save = vi.fn(); const remove = vi.fn();
vi.mock('../../api/corrections', () => ({
  listCorrections: () => list(), listCorrectionAccounts: () => accounts(),
  saveCorrection: (...args: unknown[]) => save(...args), deleteCorrection: (...args: unknown[]) => remove(...args),
  emitCorrectionsUpdated: () => window.dispatchEvent(new Event('hippo:corrections-updated')),
}));
const rule = { id: 1, source_text: 'old', replacement_text: 'new', account_biz: ['a'], enabled: true };
describe('CorrectionsPanel', () => {
  beforeEach(() => { list.mockReset().mockResolvedValue([rule]); accounts.mockReset().mockResolvedValue([{ biz: 'a', nickname: 'Alice', source: 'youtube', avatar_url: '' }]); save.mockReset(); remove.mockReset(); });
  it('updates the full rule and keeps state on a failed delete', async () => {
    save.mockResolvedValue({ ...rule, enabled: false }); remove.mockRejectedValue(new Error('offline'));
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    render(<I18nProvider><CorrectionsPanel /></I18nProvider>);
    await screen.findByText('old → new');
    fireEvent.click(screen.getByRole('checkbox', { name: '启用: old' }));
    await waitFor(() => expect(save).toHaveBeenCalledWith({ source_text: 'old', replacement_text: 'new', account_biz: ['a'], enabled: false }, 1));
    fireEvent.click(screen.getByRole('button', { name: '删除' }));
    await screen.findByText(/删除失败/);
    expect(screen.getByText('old → new')).toBeTruthy();
    vi.restoreAllMocks();
  });
  it('reports loading failure', async () => {
    list.mockRejectedValue(new Error('offline'));
    render(<I18nProvider><CorrectionsPanel /></I18nProvider>);
    expect(await screen.findByRole('alert')).toBeTruthy();
  });
});
