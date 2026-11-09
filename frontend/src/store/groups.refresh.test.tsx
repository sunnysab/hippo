import { act, renderHook } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { GroupsProvider, useGroupsActions, useGroupsState } from './groups';

const apiGetMock = vi.hoisted(() => vi.fn());
vi.mock('../api', () => ({ apiGet: apiGetMock }));
const useTestGroups = () => ({ ...useGroupsState(), ...useGroupsActions() });

describe('account refresh', () => {
  beforeEach(() => vi.clearAllMocks());

  it('loads every page before publishing the account list', async () => {
    const { result } = renderHook(useTestGroups, { wrapper: GroupsProvider });
    act(() => result.current.dispatch({ type: 'SELECT_GROUP', groupId: 7 }));
    const firstPage = Array.from({ length: 100 }, (_, index) => ({ biz: `account-${index}` }));
    let finish!: (value: unknown) => void;
    apiGetMock.mockResolvedValueOnce({ accounts: firstPage, total: 101 });
    apiGetMock.mockImplementationOnce(() => new Promise((resolve) => { finish = resolve; }));
    const controller = new AbortController();
    let request!: Promise<void>;
    await act(async () => { request = result.current.loadAccounts(true, controller.signal); });
    expect(result.current.state.accounts).toEqual([]);
    const params = new URL(apiGetMock.mock.calls[1][0], 'https://hippo.test').searchParams;
    expect(params.get('page')).toBe('2');
    expect(params.get('page_size')).toBe('100');
    expect(params.get('group_id')).toBe('7');
    expect(apiGetMock.mock.calls[1][1]).toEqual({ signal: controller.signal });
    await act(async () => {
      finish({ accounts: [{ biz: 'last-account' }], total: 101 });
      await request;
    });
    expect(result.current.state.accounts).toHaveLength(101);
    expect(result.current.state.accounts[100].biz).toBe('last-account');
  });

  it('keeps the previous complete list if a later page fails', async () => {
    const { result } = renderHook(useTestGroups, { wrapper: GroupsProvider });
    apiGetMock.mockResolvedValueOnce({ accounts: [{ biz: 'previous' }], total: 1 });
    await act(() => result.current.loadAccounts());
    apiGetMock.mockResolvedValueOnce({ accounts: [{ biz: 'partial' }], total: 2 });
    apiGetMock.mockRejectedValueOnce(new Error('Page unavailable'));
    await act(async () => {
      await expect(result.current.loadAccounts()).rejects.toThrow('Page unavailable');
    });
    expect(result.current.state.accounts.map((account) => account.biz)).toEqual(['previous']);
  });

  it('stops paging when the selected group changes', async () => {
    const { result } = renderHook(useTestGroups, { wrapper: GroupsProvider });
    apiGetMock.mockResolvedValueOnce({ accounts: [{ biz: 'first' }], total: 3 });
    let finish!: (value: unknown) => void;
    apiGetMock.mockImplementationOnce(() => new Promise((resolve) => { finish = resolve; }));
    let request!: Promise<void>;
    await act(async () => { request = result.current.loadAccounts(); });
    act(() => result.current.dispatch({ type: 'SELECT_GROUP', groupId: 2 }));
    await act(async () => {
      finish({ accounts: [{ biz: 'second' }], total: 3 });
      await request;
    });
    expect(apiGetMock).toHaveBeenCalledTimes(2);
    expect(result.current.state.accounts).toEqual([]);
  });

  it('stops if a shrinking catalogue returns an empty page', async () => {
    const { result } = renderHook(useTestGroups, { wrapper: GroupsProvider });
    apiGetMock.mockResolvedValueOnce({ accounts: [{ biz: 'first' }], total: 2 });
    apiGetMock.mockResolvedValueOnce({ accounts: [], total: 2 });
    await act(() => result.current.loadAccounts());
    expect(apiGetMock).toHaveBeenCalledTimes(2);
    expect(result.current.state.accounts.map((account) => account.biz)).toEqual(['first']);
  });

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
