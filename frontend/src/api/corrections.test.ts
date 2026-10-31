import { beforeEach, describe, expect, it, vi } from 'vitest';
import { listCorrectionAccounts } from './corrections';

const get = vi.fn();
vi.mock('./index', () => ({ apiGet: (...args: unknown[]) => get(...args), apiSend: vi.fn() }));

describe('correction accounts', () => {
  beforeEach(() => get.mockReset());
  it('filters disabled subscriptions while counting all received rows for pagination', async () => {
    get.mockResolvedValueOnce({ accounts: [{ biz: 'disabled', is_disabled: true }], total: 3 })
      .mockResolvedValueOnce({ accounts: [{ biz: 'active', is_disabled: false }, { biz: 'other', is_disabled: true }], total: 3 });
    expect(await listCorrectionAccounts()).toEqual([{ biz: 'active', is_disabled: false }]);
    expect(get).toHaveBeenCalledTimes(2);
  });
});
