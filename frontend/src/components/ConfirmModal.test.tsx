import { act, fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { ConfirmModal } from './ConfirmModal';

describe('ConfirmModal', () => {
  it('keeps a failed operation open, shows its error, and allows retrying', async () => {
    const onConfirm = vi.fn().mockRejectedValueOnce(new Error('Delete failed')).mockResolvedValueOnce(undefined);
    const onClose = vi.fn();
    render(<ConfirmModal isOpen title="Delete user" message="This is permanent" confirmLabel="Delete" onClose={onClose} onConfirm={onConfirm} />);
    expect(screen.getByRole('dialog', { name: 'Delete user' })).toBeTruthy();
    await act(async () => fireEvent.click(screen.getByRole('button', { name: 'Delete' })));
    expect(screen.getByRole('alert').textContent).toBe('Delete failed');
    expect(onClose).not.toHaveBeenCalled();
    await act(async () => fireEvent.click(screen.getByRole('button', { name: 'Delete' })));
    expect(onClose).toHaveBeenCalledOnce();
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('blocks repeated confirmation and dismissal while an operation is pending', async () => {
    let finish!: () => void;
    const onConfirm = vi.fn(() => new Promise<void>((resolve) => { finish = resolve; }));
    const onClose = vi.fn();
    const { container } = render(<ConfirmModal isOpen title="Delete user" message="This is permanent" confirmLabel="Delete" onClose={onClose} onConfirm={onConfirm} />);
    fireEvent.click(screen.getByRole('button', { name: 'Delete' }));
    fireEvent.click(screen.getByRole('button', { name: 'Delete' }));
    fireEvent.keyDown(document, { key: 'Escape' });
    fireEvent.click(container.querySelector('.modal-overlay')!);
    expect(onConfirm).toHaveBeenCalledOnce();
    expect(onClose).not.toHaveBeenCalled();
    await act(async () => finish());
    expect(onClose).toHaveBeenCalledOnce();
  });
});
