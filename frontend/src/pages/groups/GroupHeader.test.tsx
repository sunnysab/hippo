import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { I18nProvider } from '../../i18n';
import { GroupHeader } from './GroupHeader';

const state = vi.hoisted(() => ({
  groups: [{ id: 1, name: 'Default' }, { id: 2, name: 'Other' }],
  selectedGroupId: 1,
  defaultGroupId: 1,
}));
vi.mock('../../store/groups', () => ({ useGroupsState: () => ({ state }) }));
vi.mock('../../hooks/useToast', () => ({ useToast: () => ({ showToast: vi.fn() }) }));
vi.mock('react-router-dom', () => ({ useNavigate: () => vi.fn() }));

describe('group deletion availability', () => {
  it.each([1, 2])('only offers deletion for a non-default group (%s)', (groupId) => {
    state.selectedGroupId = groupId;
    const onDelete = vi.fn();
    render(<I18nProvider><GroupHeader accountQuery="" onAccountQueryChange={vi.fn()} onOpenSubscription={vi.fn()} onOpenRename={vi.fn()} onOpenDelete={onDelete} /></I18nProvider>);
    if (groupId === 1) {
      expect(screen.queryByRole('button', { name: '删除' })).toBeNull();
    } else {
      fireEvent.click(screen.getByRole('button', { name: '删除' }));
      expect(onDelete).toHaveBeenCalledWith(2);
    }
  });
});
