import { act, fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { TaskDetails } from './TaskDetails';
const get = vi.hoisted(() => vi.fn());
vi.mock('../../api', () => ({ apiGet: get }));
vi.mock('../../i18n', () => ({ useI18n: () => ({ t: (_key: string, fallback: string) => fallback }) }));

describe('task detail loading', () => {
  it('fetches account details only after expansion', async () => {
    get.mockResolvedValue({ task_id: 'job', accounts: [{ biz: 'b', nickname: 'Detailed account', status: 'success', saved: 4 }] });
    const view = render(<TaskDetails taskId="job" active={false} />);
    expect(get).not.toHaveBeenCalled();
    const details = view.container.querySelector('details')!;
    await act(async () => { details.open = true; fireEvent(details, new Event('toggle')); });
    expect(await screen.findByText('Detailed account')).toBeTruthy();
    expect(get).toHaveBeenCalledWith('/api/settings/tasks/job', expect.anything());
  });
});
