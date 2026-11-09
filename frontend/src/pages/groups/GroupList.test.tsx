import { render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { I18nProvider } from '../../i18n';
import { GroupList } from './GroupList';

const state = vi.hoisted(() => ({
  groups: [{ id: 1, name: 'Example', subscription_count: 5, disabled_count: 0 as number | undefined }],
  selectedGroupId: null,
}));
vi.mock('../../store/groups', () => ({ useGroupsState: () => ({ state }) }));
vi.mock('./GroupContextMenu', () => ({ GroupContextMenu: () => null }));

describe('group subscription summary', () => {
  it.each([0, undefined, 2])('only mentions paused subscriptions when the count is positive (%s)', (paused) => {
    state.groups[0].disabled_count = paused;
    render(<I18nProvider><GroupList onSync={vi.fn()} onSelect={vi.fn()} /></I18nProvider>);
    expect(screen.getByText(paused ? '5 个订阅，其中 2 个暂停同步。' : '5 个订阅')).toBeTruthy();
    expect(screen.queryByText(/0 个暂停同步/)).toBeNull();
  });
});
