import { act, renderHook } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { GroupsProvider, useGroupsActions, useGroupsState } from './groups';

const apiGetMock = vi.hoisted(() => vi.fn());
vi.mock('../api', () => ({ apiGet: apiGetMock }));
const useTestGroups = () => ({ ...useGroupsState(), ...useGroupsActions() });

describe('account refresh', () => {
  beforeEach(() => vi.clearAllMocks());

  it('preserves surviving selections during background refresh', async () => {
    const { result } = renderHook(useTestGroups, { wrapper: GroupsProvider });
    act(() => {
      result.current.dispatch({ type: 'SET_ACCOUNTS', accounts: [] });
      result.current.dispatch({ type: 'SET_SELECTED_ALL', bizList: ['one', 'removed'] });
    });
    apiGetMock.mockResolvedValue({ accounts: [{ biz: 'one' }] });
    await act(() => result.current.loadAccounts(true));
    expect(result.current.state.selectedAccounts).toEqual(['one']);
    await act(() => result.current.loadAccounts());
    expect(result.current.state.selectedAccounts).toEqual([]);
  });

  it('ignores a response from the previously selected group', async () => {
    const { result } = renderHook(useTestGroups, { wrapper: GroupsProvider });
    let resolve!: (value: unknown) => void;
    apiGetMock.mockReturnValue(new Promise((done) => { resolve = done; }));
    let request!: Promise<void>;
    act(() => { request = result.current.loadAccounts(true); });
    act(() => result.current.dispatch({ type: 'SELECT_GROUP', groupId: 2 }));
    await act(async () => {
      resolve({ accounts: [{ biz: 'old' }] });
      await request;
    });
    expect(result.current.state.accounts).toEqual([]);
  });

  it('ignores an older response when a newer refresh has finished', async () => {
    const { result } = renderHook(useTestGroups, { wrapper: GroupsProvider });
    let resolve!: (value: unknown) => void;
    apiGetMock.mockReturnValueOnce(new Promise((done) => { resolve = done; }));
    let request!: Promise<void>;
    act(() => { request = result.current.loadAccounts(true); });
    apiGetMock.mockResolvedValueOnce({ accounts: [{ biz: 'new' }] });
    await act(() => result.current.loadAccounts(true));
    await act(async () => {
      resolve({ accounts: [{ biz: 'old' }] });
      await request;
    });
    expect(result.current.state.accounts.map((account) => account.biz)).toEqual(['new']);
  });
  it('ignores an aborted response even if the transport still resolves', async () => {
    const { result } = renderHook(useTestGroups, { wrapper: GroupsProvider });
    const controller = new AbortController();
    let resolve!: (value: unknown) => void;
    apiGetMock.mockReturnValueOnce(new Promise((done) => { resolve = done; }));
    let request!: Promise<void>;
    act(() => { request = result.current.loadAccounts(true, controller.signal); });
    controller.abort();
    await act(async () => {
      resolve({ accounts: [{ biz: 'stale' }] });
      await request;
    });
    expect(result.current.state.accounts).toEqual([]);
  });

});
